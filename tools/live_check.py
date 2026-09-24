"""End-to-end check against a running SketchUp with the MCP extension loaded.

Unlike the pytest suite, nothing here is faked. It drives three layers:

* ``raw``   - the extension's TCP server directly, to prove framing, request id
              correlation, connection reuse and error codes on the Ruby side.
* ``mcp``   - the real MCP stdio server via the official ``mcp`` client, to prove
              initialize / tools/list / repeated tools/call.
* ``model`` - actual modelling: build a 500 mm cube, save it, then reopen the
              saved file and measure it. Only a non-empty file that reopens with
              the right geometry counts as a pass; the boolean that
              ``Model#save`` returns is recorded but never trusted on its own.

Usage (SketchUp must be running with the MCP server started):

    python tools/live_check.py --report output/fork-verification/live-check.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

HOST = os.environ.get("SKETCHUP_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("SKETCHUP_MCP_PORT", "9876"))

_results: List[Dict[str, Any]] = []


def record(name: str, ok: bool, detail: Any = None) -> bool:
    _results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail is not None else ""))
    return bool(ok)


# -- raw TCP layer ---------------------------------------------------------


def open_raw(timeout: float = 20.0) -> socket.socket:
    sock = socket.create_connection((HOST, PORT), timeout=timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


def frame(method: str, params: Optional[Dict[str, Any]], rid: Any) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}, "id": rid}).encode() + b"\n"


def tool_frame(name: str, arguments: Dict[str, Any], rid: Any) -> bytes:
    return frame("tools/call", {"name": name, "arguments": arguments}, rid)


def read_frames(sock: socket.socket, count: int, timeout: float = 30.0) -> List[Dict[str, Any]]:
    sock.settimeout(timeout)
    buffer = bytearray()
    out: List[Dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    while len(out) < count:
        newline = buffer.find(b"\n")
        if newline >= 0:
            line = bytes(buffer[:newline]).decode("utf-8", "replace").strip()
            del buffer[: newline + 1]
            if line:
                out.append(json.loads(line))
            continue
        if time.monotonic() > deadline:
            raise TimeoutError(f"got {len(out)}/{count} frames before the deadline")
        sock.settimeout(max(deadline - time.monotonic(), 0.1))
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError(f"peer closed after {len(out)}/{count} frames")
        buffer.extend(chunk)
    return out


def raw_checks() -> None:
    # 1. ping
    sock = open_raw()
    try:
        sock.sendall(frame("ping", None, 1001))
        reply = read_frames(sock, 1)[0]
        record(
            "raw/ping answered and correlated",
            reply.get("id") == 1001 and bool((reply.get("result") or {}).get("ok")),
            reply.get("result"),
        )

        # 2. several requests on ONE connection (upstream closed after each)
        ids = [1002, 1003, 1004]
        for rid in ids:
            sock.sendall(tool_frame("eval_ruby", {"code": f"{rid} * 2"}, rid))
            got = read_frames(sock, 1)[0]
            if got.get("id") != rid:
                record("raw/connection reuse", False, f"expected id {rid}, got {got.get('id')}")
                break
        else:
            record("raw/connection reuse: 4 requests, 1 connection", True, {"ids": [1001] + ids})

        # 3. a request delivered in two halves must not freeze SketchUp
        payload = tool_frame("eval_ruby", {"code": "40 + 2"}, 1005)
        split = len(payload) // 2
        sock.sendall(payload[:split])
        time.sleep(0.6)  # several UI-timer ticks with an incomplete frame pending
        sock.sendall(payload[split:])
        reply = read_frames(sock, 1)[0]
        text = ((reply.get("result") or {}).get("content") or [{}])[0].get("text")
        record("raw/partial frame across ticks", reply.get("id") == 1005 and text == "42", text)

        # 4. pipelined requests in a single write
        sock.sendall(tool_frame("eval_ruby", {"code": "1"}, 1006) + tool_frame("eval_ruby", {"code": "2"}, 1007))
        replies = read_frames(sock, 2)
        record(
            "raw/pipelined requests answered in order",
            [r.get("id") for r in replies] == [1006, 1007],
            [r.get("id") for r in replies],
        )

        # 5. malformed JSON still yields a correlated parse error
        sock.sendall(b'{"jsonrpc":"2.0","method":"tools/call","id":1008,\n')
        reply = read_frames(sock, 1)[0]
        record(
            "raw/parse error is correlated",
            reply.get("id") == 1008 and (reply.get("error") or {}).get("code") == -32700,
            reply.get("error"),
        )

        # 6. the connection survives that error
        sock.sendall(frame("ping", None, 1009))
        reply = read_frames(sock, 1)[0]
        record("raw/connection survives a bad frame", reply.get("id") == 1009, reply.get("id"))

        # 7. unknown method
        sock.sendall(frame("no/such/method", None, 1010))
        reply = read_frames(sock, 1)[0]
        record(
            "raw/unknown method -> -32601",
            (reply.get("error") or {}).get("code") == -32601 and reply.get("id") == 1010,
            reply.get("error"),
        )
    finally:
        sock.close()

    # 8. a brand-new connection after an abrupt close still works
    dead = open_raw()
    dead.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
    dead.close()  # RST, not a clean FIN
    time.sleep(0.3)
    fresh = open_raw()
    try:
        fresh.sendall(frame("ping", None, 1011))
        reply = read_frames(fresh, 1)[0]
        record("raw/server survives an aborted client", reply.get("id") == 1011, reply.get("result"))
    finally:
        fresh.close()

    # 9. two concurrent clients each get their own replies
    a, b = open_raw(), open_raw()
    try:
        a.sendall(tool_frame("eval_ruby", {"code": "'AAA'"}, 2001))
        b.sendall(tool_frame("eval_ruby", {"code": "'BBB'"}, 2002))
        ra = read_frames(a, 1)[0]
        rb = read_frames(b, 1)[0]
        text_a = ((ra.get("result") or {}).get("content") or [{}])[0].get("text")
        text_b = ((rb.get("result") or {}).get("content") or [{}])[0].get("text")
        record(
            "raw/two concurrent clients are not crossed",
            (ra.get("id"), text_a) == (2001, "AAA") and (rb.get("id"), text_b) == (2002, "BBB"),
            {"a": [ra.get("id"), text_a], "b": [rb.get("id"), text_b]},
        )
    finally:
        a.close()
        b.close()


# -- MCP stdio layer -------------------------------------------------------

NEW_MODEL_RUBY = """
require 'json'
ok = Sketchup.file_new
model = Sketchup.active_model
{
  'file_new' => ok,
  'path' => model.path,
  'modified' => model.modified?,
  'entities' => model.entities.length
}.to_json
"""

CUBE_RUBY = """
require 'json'
model = Sketchup.active_model
model.start_operation('MCP 500mm cube', true)
begin
  ents = model.entities
  ents.clear! if ents.length > 0
  group = ents.add_group
  size = 500.mm
  face = group.entities.add_face([0, 0, 0], [size, 0, 0], [size, size, 0], [0, size, 0])
  face.reverse! if face.normal.z < 0
  face.pushpull(size)
  model.commit_operation
rescue Exception => e
  model.abort_operation
  raise e
end
group = model.entities.grep(Sketchup::Group).first
bb = group.bounds
{
  'ok' => true,
  'faces' => group.entities.grep(Sketchup::Face).length,
  'edges' => group.entities.grep(Sketchup::Edge).length,
  'dims_mm' => [bb.width.to_mm.round(4), bb.height.to_mm.round(4), bb.depth.to_mm.round(4)],
  'solid' => group.manifold?,
  'volume_mm3' => (group.volume * (25.4 ** 3)).round(2),
  'model_entities' => model.entities.length
}.to_json
"""

SAVE_RUBY = """
require 'json'
require 'fileutils'
path = %PATH%
FileUtils.mkdir_p(File.dirname(path))
File.delete(path) if File.exist?(path)
model = Sketchup.active_model
error = nil
saved = nil
begin
  saved = model.save(path)
rescue Exception => e
  error = "#{e.class}: #{e.message}"
end
{
  'save_returned' => saved,
  'save_error' => error,
  'path' => path,
  'exists' => File.exist?(path),
  'size' => (File.exist?(path) ? File.size(path) : 0),
  'model_path' => model.path,
  'siblings' => Dir.glob(File.join(File.dirname(path), '*')).map { |f| [File.basename(f), File.size(f)] }
}.to_json
"""

REOPEN_RUBY = """
require 'json'
path = %PATH%
opened = Sketchup.open_file(path)
model = Sketchup.active_model
group = model.entities.grep(Sketchup::Group).first
bb = group ? group.bounds : model.bounds
{
  'opened' => opened,
  'model_path' => model.path,
  'faces' => group ? group.entities.grep(Sketchup::Face).length : nil,
  'edges' => group ? group.entities.grep(Sketchup::Edge).length : nil,
  'dims_mm' => [bb.width.to_mm.round(4), bb.height.to_mm.round(4), bb.depth.to_mm.round(4)],
  'solid' => group ? group.manifold? : nil,
  'volume_mm3' => group ? (group.volume * (25.4 ** 3)).round(2) : nil,
  'sketchup' => Sketchup.version
}.to_json
"""


def _ruby_with_path(template: str, path: str) -> str:
    return template.replace("%PATH%", json.dumps(path.replace("\\", "/")))


def _tool_payload(result: Any) -> Any:
    """Unpack the JSON string an eval_ruby tool call returns."""
    texts = [c.text for c in getattr(result, "content", []) if getattr(c, "type", "") == "text"]
    if not texts:
        return None
    outer = json.loads(texts[0])
    inner = outer.get("result") if isinstance(outer, dict) else None
    if isinstance(inner, str):
        try:
            return json.loads(inner)
        except ValueError:
            return inner
    return outer


async def mcp_checks(save_path: Path, server_command: Optional[List[str]] = None) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = os.environ.copy()
    env.setdefault("SKETCHUP_MCP_TIMEOUT", "120")
    command, *arguments = server_command or [sys.executable, "-m", "sketchup_mcp"]
    params = StdioServerParameters(command=command, args=arguments, env=env)
    print(f"--- MCP server under test: {[command, *arguments]} ---")

    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            init = await session.initialize()
            record(
                "mcp/initialize",
                bool(init.serverInfo.name),
                {"server": init.serverInfo.name, "protocol": init.protocolVersion},
            )

            listed = await session.list_tools()
            names = sorted(tool.name for tool in listed.tools)
            record("mcp/tools/list returns 10 tools", len(names) == 10, names)

            # repeated reads on the same session
            reads = []
            for _ in range(3):
                res = await session.call_tool("get_selection", {})
                reads.append(bool(res.isError))
            record("mcp/three consecutive reads succeed", not any(reads), {"isError": reads})

            # repeated mutations
            mutations = []
            for index in range(3):
                res = await session.call_tool(
                    "eval_ruby",
                    {"code": f"Sketchup.active_model.entities.length + {index}"},
                )
                mutations.append(bool(res.isError))
            record("mcp/three consecutive eval_ruby calls succeed", not any(mutations), {"isError": mutations})

            # a real Ruby failure must surface as isError, not as a success payload
            failing = await session.call_tool("eval_ruby", {"code": "raise 'deliberate failure'"})
            record(
                "mcp/a failing command reports isError",
                bool(failing.isError),
                (failing.content[0].text[:160] if failing.content else None),
            )
            # and the session is still usable afterwards
            after = await session.call_tool("get_selection", {})
            record("mcp/session usable after a failure", not after.isError)

            # -- modelling -------------------------------------------------
            # Start from a brand-new untitled document: that is the exact case
            # where Model#save previously reported success while leaving a
            # zero-byte .skp behind.
            fresh = await session.call_tool("eval_ruby", {"code": NEW_MODEL_RUBY})
            fresh_info = _tool_payload(fresh)
            record(
                "model/fresh untitled document",
                (not fresh.isError) and isinstance(fresh_info, dict) and fresh_info.get("path") == "",
                fresh_info,
            )

            built = await session.call_tool("eval_ruby", {"code": CUBE_RUBY})
            cube = _tool_payload(built)
            record(
                "model/built a 500mm cube",
                (not built.isError)
                and isinstance(cube, dict)
                and cube.get("faces") == 6
                and cube.get("edges") == 12
                and cube.get("solid") is True
                and all(abs(d - 500.0) < 0.01 for d in (cube.get("dims_mm") or [])),
                cube,
            )

            saved = await session.call_tool(
                "eval_ruby", {"code": _ruby_with_path(SAVE_RUBY, str(save_path))}
            )
            save_info = _tool_payload(saved)
            record(
                "model/save raised no exception",
                isinstance(save_info, dict) and not save_info.get("save_error"),
                save_info,
            )

            # Verified from the filesystem, not from the save() return value.
            size = save_path.stat().st_size if save_path.exists() else 0
            record("model/saved file is non-empty on disk", size > 0, {"path": str(save_path), "bytes": size})
            # The failure signature we are guarding against: a zero-byte .skp at
            # the requested path plus a leftover "<name>-0.skp" temp file that
            # SketchUp could not rename. A zero-byte .skb is normal on a first
            # save, because there is no previous version to back up.
            siblings = sorted(save_path.parent.glob(f"{save_path.stem}*"))
            bad = sorted(
                p.name
                for p in siblings
                if (p.suffix.lower() == ".skp" and p.stat().st_size == 0)
                or p.name.lower().startswith(f"{save_path.stem.lower()}-")
            )
            record(
                "model/no failed-save leftovers",
                not bad,
                {"offending": bad, "all": [(p.name, p.stat().st_size) for p in siblings]},
            )

            reopened = await session.call_tool(
                "eval_ruby", {"code": _ruby_with_path(REOPEN_RUBY, str(save_path))}
            )
            info = _tool_payload(reopened)
            record(
                "model/reopened file measures 500x500x500mm",
                isinstance(info, dict)
                and info.get("opened") is True
                and info.get("faces") == 6
                and info.get("edges") == 12
                and info.get("solid") is True
                and all(abs(d - 500.0) < 0.01 for d in (info.get("dims_mm") or []))
                and abs((info.get("volume_mm3") or 0) - 125_000_000) < 1.0,
                info,
            )

            # -- the extension restarting mid-session ----------------------
            # Schedule a restart, then stop the server from inside the request
            # being served. The reply cannot arrive, so the client must say so
            # instead of pretending, and must then reconnect by itself.
            restart = await session.call_tool(
                "eval_ruby",
                {"code": "UI.start_timer(2, false) { SU_MCP.server.start }; SU_MCP.server.stop; 'stopping'"},
            )
            restart_text = restart.content[0].text if restart.content else ""
            record(
                "transport/a lost reply is reported, not retried",
                bool(restart.isError) and "not retried" in restart_text,
                restart_text[:200],
            )
            await asyncio.sleep(6)
            back = await session.call_tool("get_selection", {})
            record(
                "transport/reconnects after the extension restarts",
                not back.isError,
                (back.content[0].text[:120] if back.content else None),
            )


# -- entry point -----------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="output/fork-verification/live-check.json")
    parser.add_argument("--skip-raw", action="store_true")
    parser.add_argument("--skip-mcp", action="store_true")
    parser.add_argument(
        "--save-path",
        default=None,
        help="where the verification cube is written (a fresh unique name by default)",
    )
    parser.add_argument(
        "--server-command",
        nargs="+",
        default=None,
        help="command that starts the MCP server (default: this interpreter -m sketchup_mcp)",
    )
    args = parser.parse_args()

    save_path = Path(
        args.save_path
        or f"output/fork-verification/Cube500_{time.strftime('%Y%m%d_%H%M%S')}.skp"
    ).resolve()

    if not args.skip_raw:
        raw_checks()
    if not args.skip_mcp:
        asyncio.run(mcp_checks(save_path, args.server_command))

    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    failed = [r for r in _results if not r["ok"]]
    report.write_text(
        json.dumps(
            {
                "sketchup_endpoint": f"{HOST}:{PORT}",
                "python": sys.version,
                "save_path": str(save_path),
                "total": len(_results),
                "failed": len(failed),
                "results": _results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed -> {report}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
