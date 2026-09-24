"""MCP server exposing the SketchUp extension over stdio.

The tool surface is unchanged from upstream. What changed is everything under
it: the TCP transport lives in :mod:`sketchup_mcp.transport`, the MCP SDK class
is resolved by :mod:`sketchup_mcp._compat` so both ``mcp`` 1.x and 2.x work, and
a failing command now raises instead of returning a success payload whose text
happens to contain the word "error".
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional

from ._compat import MCP_SDK_MAJOR, Context, McpServer
from .transport import (
    SketchupTransportError,
    close_connection,
    get_connection,
    result_text,
)

logger = logging.getLogger("sketchup_mcp.server")

__version__ = "0.2.0"

# Read-only commands are the only ones that may be re-sent after their bytes
# reached SketchUp; everything else is attempted exactly once on the wire.
_READ_ONLY_TOOLS = frozenset({"get_selection"})


def _call(ctx: Context, tool_name: str, arguments: Dict[str, Any]) -> Any:
    """Forward one tool call to SketchUp, or raise a descriptive error."""
    retry_safe = tool_name in _READ_ONLY_TOOLS
    connection = get_connection()
    logger.info(
        "%s -> SketchUp %s (mcp_request_id=%s, retry_safe=%s)",
        tool_name,
        connection.address,
        getattr(ctx, "request_id", None),
        retry_safe,
    )
    try:
        return connection.call(tool_name, arguments, retry_safe=retry_safe)
    except SketchupTransportError as exc:
        logger.error("%s failed: %s (request_sent=%s)", tool_name, exc, exc.request_sent)
        raise RuntimeError(f"{tool_name} failed: {exc}") from exc


def _ok(result: Any) -> str:
    """Serialise a successful result for the MCP client."""
    return json.dumps(result, ensure_ascii=False)


@asynccontextmanager
async def server_lifespan(server: McpServer) -> AsyncIterator[Dict[str, Any]]:
    """Report reachability at startup without leaving unread bytes behind.

    Upstream wrote a ``ping`` here and never read the reply, which desynchronised
    the very first real command. This only probes, and a failure is logged rather
    than fatal so the server can start before SketchUp does.
    """
    logger.info("sketchup-mcp %s starting (mcp SDK major %s)", __version__, MCP_SDK_MAJOR)
    connection = get_connection()
    try:
        status = connection.ping()
        logger.info("SketchUp reachable at %s: %s", connection.address, status)
    except SketchupTransportError as exc:
        logger.warning(
            "SketchUp is not reachable at %s yet (%s). "
            "Start SketchUp and its MCP Server; tools will connect on first use.",
            connection.address,
            exc,
        )
    try:
        yield {}
    finally:
        close_connection()
        logger.info("sketchup-mcp shut down")


mcp = McpServer(
    "SketchupMCP",
    instructions="Sketchup integration through the Model Context Protocol",
    lifespan=server_lifespan,
)


# -- tools -----------------------------------------------------------------


@mcp.tool()
def create_component(
    ctx: Context,
    type: str = "cube",
    position: Optional[List[float]] = None,
    dimensions: Optional[List[float]] = None,
) -> str:
    """Create a new component in Sketchup"""
    return _ok(
        _call(
            ctx,
            "create_component",
            {
                "type": type,
                "position": position or [0, 0, 0],
                "dimensions": dimensions or [1, 1, 1],
            },
        )
    )


@mcp.tool()
def delete_component(ctx: Context, id: str) -> str:
    """Delete a component by ID"""
    return _ok(_call(ctx, "delete_component", {"id": id}))


@mcp.tool()
def transform_component(
    ctx: Context,
    id: str,
    position: Optional[List[float]] = None,
    rotation: Optional[List[float]] = None,
    scale: Optional[List[float]] = None,
) -> str:
    """Transform a component's position, rotation, or scale"""
    arguments: Dict[str, Any] = {"id": id}
    if position is not None:
        arguments["position"] = position
    if rotation is not None:
        arguments["rotation"] = rotation
    if scale is not None:
        arguments["scale"] = scale
    return _ok(_call(ctx, "transform_component", arguments))


@mcp.tool()
def get_selection(ctx: Context) -> str:
    """Get currently selected components"""
    return _ok(_call(ctx, "get_selection", {}))


@mcp.tool()
def set_material(ctx: Context, id: str, material: str) -> str:
    """Set material for a component"""
    return _ok(_call(ctx, "set_material", {"id": id, "material": material}))


@mcp.tool()
def export_scene(ctx: Context, format: str = "skp") -> str:
    """Export the current scene"""
    return _ok(_call(ctx, "export", {"format": format}))


@mcp.tool()
def create_mortise_tenon(
    ctx: Context,
    mortise_id: str,
    tenon_id: str,
    width: float = 1.0,
    height: float = 1.0,
    depth: float = 1.0,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    offset_z: float = 0.0,
) -> str:
    """Create a mortise and tenon joint between two components"""
    return _ok(
        _call(
            ctx,
            "create_mortise_tenon",
            {
                "mortise_id": mortise_id,
                "tenon_id": tenon_id,
                "width": width,
                "height": height,
                "depth": depth,
                "offset_x": offset_x,
                "offset_y": offset_y,
                "offset_z": offset_z,
            },
        )
    )


@mcp.tool()
def create_dovetail(
    ctx: Context,
    tail_id: str,
    pin_id: str,
    width: float = 1.0,
    height: float = 1.0,
    depth: float = 1.0,
    angle: float = 15.0,
    num_tails: int = 3,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    offset_z: float = 0.0,
) -> str:
    """Create a dovetail joint between two components"""
    return _ok(
        _call(
            ctx,
            "create_dovetail",
            {
                "tail_id": tail_id,
                "pin_id": pin_id,
                "width": width,
                "height": height,
                "depth": depth,
                "angle": angle,
                "num_tails": num_tails,
                "offset_x": offset_x,
                "offset_y": offset_y,
                "offset_z": offset_z,
            },
        )
    )


@mcp.tool()
def create_finger_joint(
    ctx: Context,
    board1_id: str,
    board2_id: str,
    width: float = 1.0,
    height: float = 1.0,
    depth: float = 1.0,
    num_fingers: int = 5,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    offset_z: float = 0.0,
) -> str:
    """Create a finger joint (box joint) between two components"""
    return _ok(
        _call(
            ctx,
            "create_finger_joint",
            {
                "board1_id": board1_id,
                "board2_id": board2_id,
                "width": width,
                "height": height,
                "depth": depth,
                "num_fingers": num_fingers,
                "offset_x": offset_x,
                "offset_y": offset_y,
                "offset_z": offset_z,
            },
        )
    )


@mcp.tool()
def eval_ruby(ctx: Context, code: str) -> str:
    """Evaluate arbitrary Ruby code in Sketchup"""
    logger.info("eval_ruby called with code length: %d", len(code))
    result = _call(ctx, "eval_ruby", {"code": code})
    return json.dumps(
        {"success": True, "result": result_text(result, default="Success")},
        ensure_ascii=False,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    mcp.run()


if __name__ == "__main__":
    main()
