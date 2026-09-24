"""Smallest possible live smoke test of eval_ruby.

Needs SketchUp running with the MCP extension server started. It calls the tool
function directly (no MCP client), which is handy when you want to see the raw
transport error instead of an MCP `isError` wrapper.

    python examples/eval_ruby_smoke.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sketchup_mcp.server import eval_ruby


@dataclass
class MockContext:
    request_id: int = 1


CODE = """
model = Sketchup.active_model
line = model.active_entities.add_line([0, 0, 0], [100, 100, 100])
line.entityID
"""


def main() -> int:
    try:
        raw = eval_ruby(MockContext(), CODE)
    except RuntimeError as exc:
        # A failing command now raises instead of returning a success payload.
        print(f"FAILED: {exc}")
        return 1
    print(json.dumps(json.loads(raw), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
