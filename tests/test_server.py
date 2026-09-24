"""Tests for the MCP layer: SDK compatibility, tool surface, error surfacing."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest

from sketchup_mcp import _compat, server as server_module
from sketchup_mcp.transport import SketchupConnection, SketchupToolError

from .fake_sketchup import FakeSketchup

EXPECTED_TOOLS = {
    "create_component",
    "create_dovetail",
    "create_finger_joint",
    "create_mortise_tenon",
    "delete_component",
    "eval_ruby",
    "export_scene",
    "get_selection",
    "set_material",
    "transform_component",
}


@dataclass
class StubContext:
    request_id: str = "mcp-1"


@pytest.fixture
def wired(monkeypatch):
    """Point the module-level connection at a fake extension."""
    with FakeSketchup() as fake:
        connection = SketchupConnection(
            host=fake.host, port=fake.port, timeout=2.0, probe_idle_after=1e9
        )
        monkeypatch.setattr(server_module, "get_connection", lambda: connection)
        try:
            yield fake, connection
        finally:
            connection.close()


# -- SDK compatibility -----------------------------------------------------


def test_compat_binds_a_supported_sdk():
    assert _compat.MCP_SDK_MAJOR in (1, 2)
    assert _compat.FastMCP is _compat.McpServer
    assert isinstance(_compat.McpServer, type)


def test_compat_exports_context():
    assert _compat.Context is not None


def test_compat_falls_back_to_the_mcp_2_layout():
    """mcp 2.x makes mcp.server.fastmcp raise; we must land on MCPServer."""
    import builtins
    import importlib
    import sys
    import types

    fake = types.ModuleType("mcp.server.mcpserver")

    class FakeMCPServer:
        pass

    class FakeContext:
        pass

    fake.MCPServer = FakeMCPServer
    fake.Context = FakeContext

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "mcp.server.fastmcp":
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        if name == "mcp.server.mcpserver":
            return fake
        return real_import(name, globals, locals, fromlist, level)

    saved = sys.modules.pop("sketchup_mcp._compat")
    builtins.__import__ = fake_import
    try:
        reloaded = importlib.import_module("sketchup_mcp._compat")
        assert reloaded.MCP_SDK_MAJOR == 2
        assert reloaded.McpServer is FakeMCPServer
        assert reloaded.FastMCP is FakeMCPServer
        assert reloaded.Context is FakeContext
    finally:
        builtins.__import__ = real_import
        sys.modules["sketchup_mcp._compat"] = saved


def test_compat_reports_a_usable_error_when_no_sdk_is_present():
    import builtins
    import importlib
    import sys

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in ("mcp.server.fastmcp", "mcp.server.mcpserver"):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return real_import(name, globals, locals, fromlist, level)

    saved = sys.modules.pop("sketchup_mcp._compat")
    builtins.__import__ = fake_import
    try:
        with pytest.raises(ImportError) as excinfo:
            importlib.import_module("sketchup_mcp._compat")
        assert "mcp[cli]" in str(excinfo.value)
    finally:
        builtins.__import__ = real_import
        sys.modules["sketchup_mcp._compat"] = saved


def test_generated_manifest_lists_every_tool():
    import json
    from pathlib import Path

    manifest = json.loads(
        (Path(__file__).resolve().parent.parent / "sketchup.json").read_text(encoding="utf-8")
    )
    assert {tool["name"] for tool in manifest["tools"]} == EXPECTED_TOOLS


# -- tool surface ----------------------------------------------------------


def test_registers_exactly_the_documented_tools():
    tools = asyncio.run(server_module.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert names == EXPECTED_TOOLS
    assert len(tools) == 10


def test_read_only_set_is_the_only_retry_safe_one():
    assert server_module._READ_ONLY_TOOLS == frozenset({"get_selection"})


# -- behaviour through the real transport ---------------------------------


def test_create_component_forwards_arguments(wired):
    fake, _connection = wired
    raw = server_module.create_component(
        StubContext(), type="cube", position=[1, 2, 3], dimensions=[4, 5, 6]
    )
    assert json.loads(raw)["success"] is True
    call = fake.tool_calls("create_component")[0]
    assert call["params"]["arguments"] == {
        "type": "cube",
        "position": [1, 2, 3],
        "dimensions": [4, 5, 6],
    }


def test_create_component_defaults(wired):
    fake, _connection = wired
    server_module.create_component(StubContext())
    call = fake.tool_calls("create_component")[0]
    assert call["params"]["arguments"] == {
        "type": "cube",
        "position": [0, 0, 0],
        "dimensions": [1, 1, 1],
    }


def test_transform_component_omits_unset_fields(wired):
    fake, _connection = wired
    server_module.transform_component(StubContext(), id="42", position=[1, 0, 0])
    call = fake.tool_calls("transform_component")[0]
    assert call["params"]["arguments"] == {"id": "42", "position": [1, 0, 0]}


def test_export_scene_uses_the_export_tool_name(wired):
    fake, _connection = wired
    server_module.export_scene(StubContext(), format="dae")
    assert fake.tool_calls("export")[0]["params"]["arguments"] == {"format": "dae"}


def test_eval_ruby_returns_the_ruby_result_text(wired):
    fake, _connection = wired

    def responder(request, client, _server):
        if request.get("method") == "ping":
            client.reply_result(request, {"ok": True})
            return
        client.reply_text(request, "12345")

    fake.responder = responder
    parsed = json.loads(server_module.eval_ruby(StubContext(), code="1 + 1"))
    assert parsed == {"success": True, "result": "12345"}


def test_tool_failure_raises_so_the_client_sees_is_error(wired):
    """Upstream returned a success payload containing an error string."""
    fake, _connection = wired

    def responder(request, client, _server):
        client.reply_error(request, "Ruby evaluation error: boom", code=-32603)

    fake.responder = responder
    with pytest.raises(RuntimeError) as excinfo:
        server_module.eval_ruby(StubContext(), code="raise 'boom'")
    message = str(excinfo.value)
    assert "eval_ruby failed" in message
    assert "boom" in message


def test_get_selection_is_sent_as_a_read(wired):
    fake, _connection = wired
    server_module.get_selection(StubContext())
    assert fake.tool_calls("get_selection")[0]["params"]["arguments"] == {}


def test_repeated_mutations_and_reads_share_one_connection(wired):
    fake, _connection = wired
    for index in range(5):
        server_module.create_component(StubContext(), position=[index, 0, 0])
        server_module.get_selection(StubContext())
    assert fake.connection_count == 1
    assert len(fake.tool_calls("create_component")) == 5
    assert len(fake.tool_calls("get_selection")) == 5


def test_lifespan_probe_does_not_leave_unread_bytes(wired):
    """The startup probe must consume its own reply (the upstream ping bug)."""
    fake, connection = wired

    async def drive():
        async with server_module.server_lifespan(server_module.mcp):
            return None

    monkeypatched_close = []
    original = server_module.close_connection
    server_module.close_connection = lambda: monkeypatched_close.append(True)
    try:
        asyncio.run(drive())
    finally:
        server_module.close_connection = original

    assert "ping" in fake.methods()
    # The very next call must get its own reply, not the ping reply.
    raw = server_module.get_selection(StubContext())
    assert json.loads(raw)["success"] is True


def test_transport_error_is_not_swallowed(monkeypatch):
    class Boom:
        address = "127.0.0.1:9876"

        def call(self, *_args, **_kwargs):
            raise SketchupToolError("no model open", code=-32603)

    monkeypatch.setattr(server_module, "get_connection", Boom)
    with pytest.raises(RuntimeError) as excinfo:
        server_module.get_selection(StubContext())
    assert "no model open" in str(excinfo.value)
