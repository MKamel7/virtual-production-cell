"""Draw the scan cycle and the PackML state machine.

    uv run --group dev python scripts/render_diagrams.py

Writes into docs/. CI regenerates and fails on a diff, the same rule the
traceability matrix lives under, because a diagram that has quietly stopped
matching the code keeps looking authoritative in a way a stale number does not.
"""

from __future__ import annotations

import sys
from pathlib import Path

from vpc.diagrams import DIAGRAMS

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs"


def main() -> int:
    OUT.mkdir(exist_ok=True)
    for name, build in DIAGRAMS.items():
        target = OUT / name
        target.write_text(build(), encoding="utf-8")
        print(f"wrote {target.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
