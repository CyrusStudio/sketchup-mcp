"""Sketchup integration through Model Context Protocol"""

from typing import Any

__version__ = "0.2.0"

__all__ = ["__version__", "mcp"]


def __getattr__(name: str) -> Any:
    """Expose ``sketchup_mcp.mcp`` without building the server on every import.

    Importing the package used to construct the MCP server as a side effect, so
    anything that only wanted :mod:`sketchup_mcp.transport` still needed a
    working MCP SDK. Resolving it lazily keeps that import cheap and testable.
    """
    if name == "mcp":
        from .server import mcp

        return mcp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
