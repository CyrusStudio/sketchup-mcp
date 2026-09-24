"""Line-framed JSON-RPC transport to the SketchUp extension.

Wire protocol (unchanged from upstream, just implemented properly on both ends):
one JSON object per line, ``\\n`` terminated, in each direction, over TCP on
``127.0.0.1:9876``.

What this module fixes relative to upstream ``server.py``:

* **Framing.** Responses are read up to the newline delimiter and any surplus
  bytes are kept in a buffer, so a reply split over several TCP segments, or two
  replies delivered in one segment, both parse correctly.
* **Stale sockets.** Upstream tested liveness with ``sock.send(b"")``, which
  succeeds on a socket the peer has already closed, and then "pinged" by writing
  a request whose reply was never read, so the *next* call read the ping reply
  and reported an unrelated error. Here liveness is a real ``MSG_PEEK`` check
  plus, for an idle reused socket, a ping whose reply *is* consumed.
* **Request/response correlation.** Ids are allocated by this connection, never
  taken from the MCP request context, and a reply whose id does not match the
  outstanding request is discarded instead of being returned as the answer.
* **No duplicated mutations.** A request is only ever re-sent when the failure
  happened strictly *before* any request byte was written, or when the caller
  marked the command ``retry_safe`` (read-only). Everything else fails loudly
  with ``request_sent=True``.
"""

from __future__ import annotations

import json
import logging
import os
import select
import socket
import threading
import time
from itertools import count
from typing import Any, Dict, Optional

logger = logging.getLogger("sketchup_mcp.transport")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9876
DEFAULT_TIMEOUT = 15.0
#: A reused socket idle for at least this long is ping-probed before a command
#: is written to it, so that a dead peer becomes a safe pre-send failure.
DEFAULT_PROBE_IDLE_AFTER = 2.0
DEFAULT_PROBE_TIMEOUT = 5.0
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_STALE_FRAMES = 32
_RECV_SIZE = 65536

NOT_RETRIED_HINT = (
    "The command had already been written to SketchUp and was deliberately not "
    "retried, so it may have been applied exactly once. Inspect the model before "
    "re-running it."
)


class SketchupTransportError(RuntimeError):
    """Base class for every transport failure.

    ``request_sent`` is ``True`` when request bytes had already been handed to
    the kernel, i.e. when the command may have taken effect in SketchUp.
    """

    def __init__(self, message: str, *, request_sent: bool = False) -> None:
        super().__init__(message)
        self.request_sent = bool(request_sent)


class SketchupConnectionError(SketchupTransportError):
    """Could not reach the extension, or lost the connection to it."""


class SketchupTimeoutError(SketchupTransportError):
    """The extension did not answer within the timeout."""


class SketchupProtocolError(SketchupTransportError):
    """The extension sent something that is not a usable JSON-RPC frame."""


class SketchupToolError(SketchupTransportError):
    """The extension answered with a JSON-RPC ``error`` member."""

    def __init__(self, message: str, *, code: Any = None, data: Any = None) -> None:
        super().__init__(message, request_sent=True)
        self.code = code
        self.data = data


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring %s=%r: not a number", name, raw)
        return default
    if value <= 0:
        logger.warning("Ignoring %s=%r: must be positive", name, raw)
        return default
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring %s=%r: not an integer", name, raw)
        return default


def _same_id(left: Any, right: Any) -> bool:
    """Compare JSON-RPC ids tolerantly (some peers echo 1 as the string "1")."""
    if left == right:
        return True
    if left is None or right is None:
        return False
    return str(left) == str(right)


def result_text(result: Any, default: str = "") -> str:
    """Pull the human-readable payload out of a tool result."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts = [
                item["text"]
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            if parts:
                return "\n".join(parts)
        for key in ("text", "result", "value"):
            value = result.get(key)
            if isinstance(value, str):
                return value
    return default


class SketchupConnection:
    """A single, lazily established connection to the SketchUp extension."""

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        timeout: Optional[float] = None,
        probe_idle_after: Optional[float] = None,
        probe_timeout: Optional[float] = None,
    ) -> None:
        self.host = host or os.environ.get("SKETCHUP_MCP_HOST") or DEFAULT_HOST
        self.port = int(port) if port is not None else _env_int("SKETCHUP_MCP_PORT", DEFAULT_PORT)
        self.timeout = (
            float(timeout)
            if timeout is not None
            else _env_float("SKETCHUP_MCP_TIMEOUT", DEFAULT_TIMEOUT)
        )
        self.probe_idle_after = (
            float(probe_idle_after)
            if probe_idle_after is not None
            else _env_float("SKETCHUP_MCP_PROBE_IDLE_AFTER", DEFAULT_PROBE_IDLE_AFTER)
        )
        self.probe_timeout = (
            float(probe_timeout)
            if probe_timeout is not None
            else _env_float("SKETCHUP_MCP_PROBE_TIMEOUT", DEFAULT_PROBE_TIMEOUT)
        )
        self._sock: Optional[socket.socket] = None
        self._buffer = bytearray()
        self._ids = count(1)
        self._last_activity = 0.0
        self._lock = threading.RLock()

    # -- lifecycle ---------------------------------------------------------

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def connect(self) -> None:
        """Open a fresh socket. No-op when one is already open."""
        with self._lock:
            if self._sock is not None:
                return
            try:
                sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            except OSError as exc:
                raise SketchupConnectionError(
                    f"Could not connect to the SketchUp extension at {self.address}: {exc}. "
                    "Start SketchUp, then Extensions (or Plugins) > MCP Server > Start Server."
                ) from exc
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:  # pragma: no cover - platform dependent
                pass
            self._sock = sock
            self._buffer.clear()
            self._last_activity = time.monotonic()
            logger.info("Connected to SketchUp at %s", self.address)

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    #: Upstream name, kept so existing callers and examples keep working.
    disconnect = close

    def _close_locked(self) -> None:
        sock, self._sock = self._sock, None
        self._buffer.clear()
        if sock is None:
            return
        try:
            sock.close()
        except OSError as exc:  # pragma: no cover - close rarely fails
            logger.debug("Error closing SketchUp socket: %s", exc)

    def is_connected(self) -> bool:
        """True when a socket is open and the peer has not closed its end."""
        with self._lock:
            return self._sock is not None and self._peer_state_locked() != "closed"

    # -- public API --------------------------------------------------------

    def ping(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Round-trip a ``ping``. Raises on failure; any correlated reply counts.

        An extension that predates the ``ping`` handler answers ``-32601 Method
        not found``, which still proves the connection is usable, so that is
        reported as ``supported: False`` rather than as an error.
        """
        probe_timeout = timeout or self.probe_timeout
        with self._lock:
            self._ensure_ready_locked(probe_timeout, allow_probe=False)
            message = self._roundtrip_locked("ping", None, probe_timeout)
        error = message.get("error")
        if isinstance(error, dict) and error.get("code") == -32601:
            return {"alive": True, "supported": False}
        if error is not None:
            raise SketchupToolError(
                str(error.get("message") if isinstance(error, dict) else error),
                code=error.get("code") if isinstance(error, dict) else None,
            )
        result = message.get("result")
        payload: Dict[str, Any] = {"alive": True, "supported": True}
        if isinstance(result, dict):
            payload.update(result)
        return payload

    def call(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        retry_safe: bool = False,
        timeout: Optional[float] = None,
    ) -> Any:
        """Invoke ``tool_name`` in SketchUp and return its JSON-RPC ``result``.

        ``retry_safe=True`` marks the command as free of side effects, which is
        the *only* condition under which it may be re-sent after the bytes went
        out. Everything else is attempted once on the wire.
        """
        call_timeout = self.timeout if timeout is None else float(timeout)
        params = {"name": tool_name, "arguments": dict(arguments or {})}

        with self._lock:
            pending: Optional[SketchupTransportError] = None
            for attempt in (1, 2):
                # Phase 1: get a usable socket. Failing here means nothing was
                # written, so retrying can never duplicate a mutation.
                try:
                    self._ensure_ready_locked(call_timeout)
                except SketchupTransportError as exc:
                    self._close_locked()
                    if attempt == 1:
                        pending = exc
                        logger.info("Preparing the connection failed (%s); reconnecting", exc)
                        continue
                    raise

                # Phase 2: write and read. Any failure from here on may already
                # have reached SketchUp.
                try:
                    message = self._roundtrip_locked("tools/call", params, call_timeout)
                except SketchupTransportError as exc:
                    self._close_locked()
                    if attempt == 1 and retry_safe:
                        pending = exc
                        logger.info(
                            "Read-only call failed (%s); retrying on a new connection", exc
                        )
                        continue
                    if exc.request_sent:
                        raise type(exc)(
                            f"{exc} {NOT_RETRIED_HINT}", request_sent=True
                        ) from exc
                    raise
                return self._unwrap(message)

            assert pending is not None  # pragma: no cover - loop always sets it
            raise pending

    # -- internals ---------------------------------------------------------

    def _ensure_ready_locked(self, timeout: float, allow_probe: bool = True) -> None:
        if self._sock is None:
            self.connect()
            return

        state = self._peer_state_locked()
        if state == "closed":
            logger.info("SketchUp closed the previous connection; reconnecting")
            self._close_locked()
            self.connect()
            return
        if state == "data" and not self._discard_unsolicited_locked():
            self._close_locked()
            self.connect()
            return

        if not allow_probe:
            return
        if time.monotonic() - self._last_activity < self.probe_idle_after:
            return
        probe_timeout = min(self.probe_timeout, timeout) if timeout > 0 else self.probe_timeout
        try:
            self._roundtrip_locked("ping", None, probe_timeout)
        except SketchupTransportError as exc:
            logger.info("Liveness probe failed (%s); reconnecting before sending", exc)
            self._close_locked()
            self.connect()

    def _peer_state_locked(self) -> str:
        """Return "closed", "data" (unsolicited bytes pending) or "idle"."""
        sock = self._sock
        if sock is None:
            return "closed"
        if self._buffer:
            return "data"
        try:
            readable, _, _ = select.select([sock], [], [], 0)
        except (OSError, ValueError):
            return "closed"
        if not readable:
            return "idle"
        try:
            peeked = sock.recv(1, socket.MSG_PEEK)
        except (BlockingIOError, socket.timeout):
            return "idle"
        except OSError:
            return "closed"
        return "data" if peeked else "closed"

    def _discard_unsolicited_locked(self) -> bool:
        """Drop bytes that arrived with no request outstanding.

        Returns ``False`` if the peer turned out to be closed.
        """
        sock = self._sock
        dropped = len(self._buffer)
        self._buffer.clear()
        alive = True
        while sock is not None:
            try:
                readable, _, _ = select.select([sock], [], [], 0)
            except (OSError, ValueError):
                alive = False
                break
            if not readable:
                break
            try:
                chunk = sock.recv(_RECV_SIZE)
            except (BlockingIOError, socket.timeout):
                break
            except OSError:
                alive = False
                break
            if not chunk:
                alive = False
                break
            dropped += len(chunk)
        if dropped:
            logger.warning(
                "Discarded %d stale byte(s) from SketchUp that belonged to an earlier request",
                dropped,
            )
        return alive

    def _roundtrip_locked(
        self,
        method: str,
        params: Optional[Dict[str, Any]],
        timeout: float,
    ) -> Dict[str, Any]:
        request_id = next(self._ids)
        request: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": {} if params is None else params,
            "id": request_id,
        }
        self._send_locked(request)
        return self._await_response_locked(request_id, timeout)

    def _send_locked(self, request: Dict[str, Any]) -> None:
        payload = json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"
        sock = self._sock
        if sock is None:  # pragma: no cover - guarded by _ensure_ready_locked
            raise SketchupConnectionError("Not connected to SketchUp")
        logger.debug("-> %s id=%s (%d bytes)", request["method"], request["id"], len(payload))
        try:
            sock.settimeout(self.timeout)
            sock.sendall(payload)
        except OSError as exc:
            # sendall may have delivered a prefix before failing, so be
            # conservative and treat the request as possibly sent.
            raise SketchupConnectionError(
                f"Failed to send {request['method']} to SketchUp: {exc}", request_sent=True
            ) from exc
        self._last_activity = time.monotonic()

    def _await_response_locked(self, request_id: Any, timeout: float) -> Dict[str, Any]:
        deadline = time.monotonic() + max(timeout, 0.0)
        stale = 0
        while True:
            frame = self._read_frame_locked(deadline, timeout)
            text = frame.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                stale += 1
                logger.warning("Ignoring unparsable frame from SketchUp: %.200s", text)
            else:
                if not isinstance(message, dict):
                    stale += 1
                    logger.warning("Ignoring non-object frame from SketchUp: %.200s", text)
                elif not _same_id(message.get("id"), request_id):
                    stale += 1
                    logger.warning(
                        "Ignoring stale reply id=%r while waiting for id=%r",
                        message.get("id"),
                        request_id,
                    )
                else:
                    self._last_activity = time.monotonic()
                    logger.debug("<- id=%s (%d bytes)", request_id, len(frame))
                    return message
            if stale > MAX_STALE_FRAMES:
                raise SketchupProtocolError(
                    f"Gave up after {stale} unusable frames from SketchUp while waiting for "
                    f"reply id={request_id!r}",
                    request_sent=True,
                )

    def _read_frame_locked(self, deadline: float, timeout: float) -> bytes:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                frame = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                return frame
            if len(self._buffer) > MAX_FRAME_BYTES:
                raise SketchupProtocolError(
                    f"SketchUp sent more than {MAX_FRAME_BYTES} bytes without a newline delimiter",
                    request_sent=True,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SketchupTimeoutError(
                    f"SketchUp did not reply within {timeout:g}s", request_sent=True
                )
            sock = self._sock
            if sock is None:  # pragma: no cover - guarded by callers
                raise SketchupConnectionError(
                    "The connection to SketchUp was closed", request_sent=True
                )
            try:
                sock.settimeout(remaining)
                chunk = sock.recv(_RECV_SIZE)
            except socket.timeout as exc:
                raise SketchupTimeoutError(
                    f"SketchUp did not reply within {timeout:g}s", request_sent=True
                ) from exc
            except OSError as exc:
                raise SketchupConnectionError(
                    f"Lost the connection to SketchUp while waiting for a reply: {exc}",
                    request_sent=True,
                ) from exc
            if not chunk:
                # Tolerate a peer that closes without a trailing newline.
                if self._buffer.strip():
                    frame = bytes(self._buffer)
                    self._buffer.clear()
                    return frame
                raise SketchupConnectionError(
                    "SketchUp closed the connection before replying", request_sent=True
                )
            self._buffer.extend(chunk)

    @staticmethod
    def _unwrap(message: Dict[str, Any]) -> Any:
        error = message.get("error")
        if error is not None:
            if isinstance(error, dict):
                raise SketchupToolError(
                    str(error.get("message") or "Unknown error from SketchUp"),
                    code=error.get("code"),
                    data=error.get("data"),
                )
            raise SketchupToolError(str(error))
        if "result" not in message:
            raise SketchupProtocolError(
                "SketchUp replied without a result or error member", request_sent=True
            )
        result = message["result"]
        return {} if result is None else result


# -- process-wide connection ----------------------------------------------

_connection: Optional[SketchupConnection] = None
_connection_lock = threading.Lock()


def get_connection(**kwargs: Any) -> SketchupConnection:
    """Return the shared connection, creating (but not opening) it on demand."""
    global _connection
    with _connection_lock:
        if _connection is None:
            _connection = SketchupConnection(**kwargs)
        return _connection


def close_connection() -> None:
    global _connection
    with _connection_lock:
        if _connection is not None:
            _connection.close()
            _connection = None


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_TIMEOUT",
    "NOT_RETRIED_HINT",
    "SketchupConnection",
    "SketchupConnectionError",
    "SketchupProtocolError",
    "SketchupTimeoutError",
    "SketchupToolError",
    "SketchupTransportError",
    "close_connection",
    "get_connection",
    "result_text",
]
