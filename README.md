# SketchupMCP - Sketchup Model Context Protocol Integration

Drive SketchUp from an MCP client (Claude, Codex, or anything else that speaks
MCP) over a local socket: create and transform geometry, apply materials, export,
and run arbitrary Ruby in the running SketchUp instance.

This is a maintained fork of [mhyrr/sketchup-mcp](https://github.com/mhyrr/sketchup-mcp),
which was itself inspired by [Blender MCP](https://github.com/ahujasid/blender-mcp).
See [NOTICE.md](NOTICE.md) for provenance and licensing.

## What this fork fixes

Upstream worked for a single command and then became unreliable. The tool surface
is unchanged; the plumbing under it was rewritten.

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ImportError: No module named 'mcp.server.fastmcp'` on a fresh install | `mcp[cli]>=1.3.0` with no upper bound resolves to mcp 2.x, which renamed `FastMCP` to `MCPServer` | dependency pinned to `mcp[cli]>=1.9.0,<2`; `sketchup_mcp._compat` binds either generation |
| First command fails with `Method not found`, the next one works | the client wrote a `ping` and never read the reply, so every later call read the *previous* call's reply | the liveness probe now consumes its reply, and replies are matched by request id |
| Commands fail after the first one | the Ruby server closed the socket after every request while the client reused it | the Ruby server keeps connections open and handles many requests per connection |
| Large or slow replies truncated / `Incomplete JSON response` | the client guessed at message boundaries by trying `json.loads` on whatever had arrived | strict `\n` framing with a carry-over buffer on both ends |
| SketchUp freezing on a partial request | the Ruby server called the blocking `client.gets` on the UI thread | fully non-blocking accept/read loop, buffered across UI-timer ticks |
| A modelling command running twice | the client silently re-sent the request on any timeout | a request is re-sent only if the failure happened *before* any byte was written, or if the command is read-only |
| `Model#save` returning `true` while leaving a zero-byte `.skp` and a `<name>-0.skp` | `Model#save` pumps the Windows message loop, which re-entered the UI timer and started a second save | the timer callback is guarded against re-entrancy |
| A failed command reported as `isError: false` with the error in the text | tools caught every exception and returned it as a success payload | tools raise, so the MCP client sees `isError: true` |

## Components

1. **SketchUp extension** (`su_mcp/`) — a TCP server inside SketchUp
   (`127.0.0.1:9876` by default) that executes JSON-RPC commands on the model.
2. **MCP server** (`src/sketchup_mcp/`) — a Python stdio MCP server that
   forwards tool calls to the extension.
   * `transport.py` — the framed JSON-RPC client, including all retry rules.
   * `_compat.py` — mcp 1.x / 2.x import shim.
   * `server.py` — the MCP tool definitions.

## Install

### Python side

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/CyrusStudio/sketchup-mcp.git
cd sketchup-mcp
uv venv
uv pip install -e .            # add '-e .[dev]' for the test suite
```

Verify the import resolves against mcp 1.x:

```bash
.venv/Scripts/python -c "import sketchup_mcp._compat as c; print(c.MCP_SDK_MAJOR)"   # -> 1
```

### SketchUp extension

Copy the two paths below into your SketchUp `Plugins` folder, then restart
SketchUp:

| From | To (Windows, SketchUp 2020) |
| --- | --- |
| `su_mcp.rb` | `%APPDATA%\SketchUp\SketchUp 2020\SketchUp\Plugins\su_mcp.rb` |
| `su_mcp/su_mcp/main.rb` | `%APPDATA%\SketchUp\SketchUp 2020\SketchUp\Plugins\su_mcp\main.rb` |

On macOS the folder is `~/Library/Application Support/SketchUp <year>/SketchUp/Plugins`.

## Run

1. In SketchUp: **Extensions > MCP Server > Start Server**. *Server Status* shows
   the address and the number of connected clients.
2. Point your MCP client at the console script inside this checkout's venv. That
   is one process with no resolver step, so the server starts instantly.

Claude Code (available in every project):

```bash
claude mcp add sketchup -s user -e SKETCHUP_MCP_TIMEOUT=60 -- \
  "C:\path\to\sketchup-mcp\.venv\Scripts\sketchup-mcp.exe"
claude mcp list          # expect: sketchup: ... - Connected
```

Codex:

```bash
codex mcp add sketchup --env SKETCHUP_MCP_TIMEOUT=60 -- \
  "C:\path\to\sketchup-mcp\.venv\Scripts\sketchup-mcp.exe"
codex mcp list
```

Any other client, as JSON (on macOS/Linux the path is `.venv/bin/sketchup-mcp`):

```json
{
  "mcpServers": {
    "sketchup": {
      "type": "stdio",
      "command": "C:/path/to/sketchup-mcp/.venv/Scripts/sketchup-mcp.exe",
      "args": [],
      "env": { "SKETCHUP_MCP_TIMEOUT": "60" }
    }
  }
}
```

Define the server in one scope only. Claude Code reports a "Conflicting scopes"
diagnostic if the same name exists in both user and project scope with different
command strings.

The MCP server starts whether or not SketchUp is up; it connects on first use and
reconnects by itself if SketchUp restarts.

### Environment variables

| Variable | Side | Default | Meaning |
| --- | --- | --- | --- |
| `SKETCHUP_MCP_HOST` / `SKETCHUP_MCP_PORT` | Python | `127.0.0.1` / `9876` | where the extension listens |
| `SKETCHUP_MCP_TIMEOUT` | Python | `15` | seconds to wait for a reply (raise it for long exports) |
| `SKETCHUP_MCP_PROBE_IDLE_AFTER` | Python | `2` | probe a reused socket idle for this long before sending |
| `SU_MCP_HOST` / `SU_MCP_PORT` | Ruby | `127.0.0.1` / `9876` | where the extension binds |
| `SU_MCP_AUTOSTART` | Ruby | off | `1` starts the server without using the menu |
| `SU_MCP_LOG_FILE` | Ruby | unset | mirror the Ruby Console log to a file |

## Tools

`create_component`, `delete_component`, `transform_component`, `get_selection`,
`set_material`, `export_scene`, `create_mortise_tenon`, `create_dovetail`,
`create_finger_joint`, `eval_ruby`.

Only `get_selection` is treated as read-only. Every other tool is a mutation and
is therefore never re-sent automatically: if it fails after the request went out,
the error says so explicitly rather than risking a duplicate operation.

`sketchup.json` is a generated snapshot of this list — regenerate it rather than
editing it by hand.

## Tests

```bash
uv pip install -e ".[dev]"
.venv/Scripts/python -m pytest              # 43 tests, no SketchUp needed
```

`tests/fake_sketchup.py` is a scriptable stand-in for the extension, so the ugly
cases (split frames, two replies in one segment, replies for the wrong id, dead
sockets, timeouts) are covered without a GUI.

### End-to-end check against a real SketchUp

With SketchUp running and the MCP server started:

```bash
.venv/Scripts/python tools/live_check.py --report output/fork-verification/live-check.json
```

It exercises the raw socket protocol, the MCP stdio session, and then actually
builds a 500 mm cube, saves it, and reopens the saved file to measure it. Exit
code 0 means every check passed; the report lists each one.

For a scripted launch, `tools/su_startup.rb` can be passed to
`SketchUp.exe -RubyStartup`: it starts the server and writes a status JSON to
`SU_MCP_STATUS_FILE`. Passing an existing `.skp` on the command line skips the
Welcome screen, which otherwise blocks the UI timer the server runs on.

## Troubleshooting

* **`Could not connect ... Start SketchUp`** — SketchUp is not running, or the
  server was not started from the Extensions menu.
* **Nothing responds but the port is open** — SketchUp is blocked by a modal
  dialog (Welcome screen, save error). UI timers do not run then; dismiss it.
* **Commands time out** — raise `SKETCHUP_MCP_TIMEOUT`; long exports and boolean
  operations can exceed 15 s.
* **Ruby errors** — set `SU_MCP_LOG_FILE` and read the log, or open the Ruby
  Console in SketchUp.

## Protocol

One JSON object per line, `\n` terminated, in both directions:

```
--> {"jsonrpc":"2.0","method":"tools/call","params":{"name":"eval_ruby","arguments":{"code":"1+1"}},"id":7}
<-- {"jsonrpc":"2.0","result":{"content":[{"type":"text","text":"2"}],"isError":false,"success":true},"id":7}
```

`ping` is also accepted and answers `{"ok":true,...}`. Ids are echoed, so a client
can and should correlate replies.

## License

MIT, as declared upstream. See [NOTICE.md](NOTICE.md).
