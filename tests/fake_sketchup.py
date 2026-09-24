"""A scriptable stand-in for the SketchUp extension's TCP server.

It speaks the same wire protocol as ``su_mcp/su_mcp/main.rb`` (one JSON object
per line, in both directions) and lets a test decide, per request, exactly what
bytes come back and when. That is what makes the nasty cases testable without
SketchUp: replies split across segments, two replies in one segment, replies for
the wrong id, silence until the timeout, and a peer that closes mid-stream.

Every request it sees is recorded, so a test can assert that a mutation reached
the server exactly once.
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Any, Callable, Dict, List, Optional

_RECV = 65536


class FakeClient:
    """The server side of one accepted connection."""

    def __init__(self, sock: socket.socket, index: int) -> None:
        self.sock = sock
        self.index = index

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def send_json(self, payload: Dict[str, Any], newline: bool = True) -> None:
        blob = json.dumps(payload).encode("utf-8")
        self.send_raw(blob + b"\n" if newline else blob)

    def reply_result(self, request: Dict[str, Any], result: Any) -> None:
        self.send_json({"jsonrpc": "2.0", "result": result, "id": request.get("id")})

    def reply_text(self, request: Dict[str, Any], text: str) -> None:
        self.reply_result(
            request,
            {
                "content": [{"type": "text", "text": text}],
                "isError": False,
                "success": True,
                "resourceId": None,
            },
        )

    def reply_error(
        self, request: Dict[str, Any], message: str, code: int = -32603
    ) -> None:
        self.send_json(
            {
                "jsonrpc": "2.0",
                "error": {"code": code, "message": message, "data": {"success": False}},
                "id": request.get("id"),
            }
        )

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


Responder = Callable[[Dict[str, Any], FakeClient, "FakeSketchup"], None]


def default_responder(request: Dict[str, Any], client: FakeClient, server: "FakeSketchup") -> None:
    """Answer like a healthy extension: ping pongs, tools echo their arguments."""
    method = request.get("method")
    if method == "ping":
        client.reply_result(request, {"ok": True, "success": True, "server": "fake"})
        return
    params = request.get("params") or {}
    client.reply_text(request, json.dumps({"tool": params.get("name"), "arguments": params.get("arguments")}))


class FakeSketchup:
    """Threaded fake extension server. Use as a context manager."""

    def __init__(self, responder: Optional[Responder] = None) -> None:
        self.responder: Responder = responder or default_responder
        self.requests: List[Dict[str, Any]] = []
        self.connection_count = 0
        self._lock = threading.Lock()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.host, self.port = self._listener.getsockname()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._clients: List[FakeClient] = []
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "FakeSketchup":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.stop()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._listener.close()
        except OSError:
            pass
        for client in list(self._clients):
            client.close()
        for thread in list(self._threads):
            thread.join(timeout=2.0)

    # -- introspection -----------------------------------------------------

    def tool_calls(self, name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Recorded ``tools/call`` requests, optionally filtered by tool name."""
        with self._lock:
            calls = [r for r in self.requests if r.get("method") == "tools/call"]
        if name is None:
            return calls
        return [r for r in calls if ((r.get("params") or {}).get("name")) == name]

    def methods(self) -> List[str]:
        with self._lock:
            return [str(r.get("method")) for r in self.requests]

    # -- server loop -------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                sock, _addr = self._listener.accept()
            except OSError:
                return
            with self._lock:
                self.connection_count += 1
                index = self.connection_count
            client = FakeClient(sock, index)
            self._clients.append(client)
            thread = threading.Thread(target=self._serve, args=(client,), daemon=True)
            self._threads.append(thread)
            thread.start()

    def _serve(self, client: FakeClient) -> None:
        buffer = bytearray()
        try:
            while not self._stop.is_set():
                try:
                    chunk = client.sock.recv(_RECV)
                except OSError:
                    return
                if not chunk:
                    return
                buffer.extend(chunk)
                while True:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        break
                    line = bytes(buffer[:newline])
                    del buffer[: newline + 1]
                    text = line.decode("utf-8", "replace").strip()
                    if not text:
                        continue
                    try:
                        request = json.loads(text)
                    except ValueError:
                        continue
                    with self._lock:
                        self.requests.append(request)
                    try:
                        self.responder(request, client, self)
                    except OSError:
                        return
        finally:
            client.close()
