"""Import shim for the two incompatible generations of the ``mcp`` Python SDK.

``mcp`` 1.x exposes the ergonomic server as ``mcp.server.fastmcp.FastMCP``.
``mcp`` 2.x renamed it to ``mcp.server.mcpserver.MCPServer`` and turned the old
module into a stub that raises ``ModuleNotFoundError`` on import.  Both classes
accept the constructor keywords (``name``, ``instructions``, ``lifespan``), the
``@server.tool()`` decorator and the ``Context`` parameter annotation that this
project uses, so we simply bind whichever generation is installed.

``MCP_SDK_MAJOR`` records which one that was, for logging and for tests.
"""

from __future__ import annotations

MCP_SDK_MAJOR: int

try:  # mcp 1.x
    from mcp.server.fastmcp import Context as Context  # noqa: F401
    from mcp.server.fastmcp import FastMCP as McpServer

    MCP_SDK_MAJOR = 1
except ModuleNotFoundError as _v1_error:  # mcp 2.x, or no mcp at all
    try:
        from mcp.server.mcpserver import Context as Context  # noqa: F401
        from mcp.server.mcpserver import MCPServer as McpServer

        MCP_SDK_MAJOR = 2
    except ModuleNotFoundError as _v2_error:  # pragma: no cover - install problem
        raise ImportError(
            "Could not import an MCP server class from the installed 'mcp' package. "
            "Neither mcp 1.x (mcp.server.fastmcp.FastMCP) nor mcp 2.x "
            "(mcp.server.mcpserver.MCPServer) is importable. Install the dependency "
            "with 'uv pip install \"mcp[cli]>=1.9,<3\"'. "
            f"v1 import said: {_v1_error}. v2 import said: {_v2_error}."
        ) from _v2_error

# Historical alias: this project (and upstream) referred to the class as FastMCP.
FastMCP = McpServer

__all__ = ["Context", "FastMCP", "McpServer", "MCP_SDK_MAJOR"]
