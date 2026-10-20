"""Write docs/OPERATOR_DEV.md from the curated operator map.

Deterministic: it renders the topic map only, never the live index, so a
refresh does not create churn in the committed document.  Run with
``.venv/bin/python scripts/render_operator_guide.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from metax_docs_mcp import opdev  # noqa: E402


def main() -> int:
    target = ROOT / "docs" / "OPERATOR_DEV.md"
    target.write_text(opdev.render_markdown(), encoding="utf-8")
    print(f"wrote {target} ({len(opdev.TOPICS)} topics)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
