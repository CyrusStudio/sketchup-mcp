"""Regenerate sketchup.json from the tools the server actually registers.

The checked-in manifest had drifted to six of the ten tools, with hand-written
schemas. Generating it from `mcp.list_tools()` keeps it honest.

    python tools/gen_manifest.py            # writes sketchup.json
    python tools/gen_manifest.py --check    # non-zero exit if it is stale
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent.parent / "sketchup.json"


def build() -> dict:
    from sketchup_mcp.server import mcp

    tools = asyncio.run(mcp.list_tools())
    return {
        "name": "sketchup",
        "description": "Sketchup integration through Model Context Protocol",
        "package": "sketchup-mcp",
        "module": "sketchup_mcp.server",
        "object": "mcp",
        "generated_by": "tools/gen_manifest.py",
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.inputSchema,
            }
            for tool in sorted(tools, key=lambda t: t.name)
        ],
        "mcpServers": {"sketchup": {"command": "uvx", "args": ["sketchup-mcp"]}},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="only report staleness")
    args = parser.parse_args()

    wanted = json.dumps(build(), indent=2, ensure_ascii=False) + "\n"
    current = MANIFEST.read_text(encoding="utf-8") if MANIFEST.exists() else ""
    if args.check:
        if wanted == current:
            print(f"{MANIFEST.name} is up to date")
            return 0
        print(f"{MANIFEST.name} is stale; run tools/gen_manifest.py")
        return 1
    MANIFEST.write_text(wanted, encoding="utf-8")
    print(f"wrote {MANIFEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
