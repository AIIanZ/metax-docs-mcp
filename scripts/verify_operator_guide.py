"""Check that every curated operator-guide reference resolves in an index.

Usage: .venv/bin/python scripts/verify_operator_guide.py [DB]

Exit code 0 means every reference matched at least one indexed document and
the report was written to stdout.  A missing reference is a hard failure: the
guide must never advertise a document the index cannot serve.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from metax_docs_mcp import opdev  # noqa: E402
from metax_docs_mcp.store import Store  # noqa: E402


def main(argv: list[str]) -> int:
    db = Path(argv[1]).expanduser() if len(argv) > 1 else ROOT / ".data/index.sqlite3"
    store = Store(db, readonly=True)
    report: dict[str, object] = {
        "database": str(db),
        "topics": [],
        "missing": [],
        "reference_count": 0,
    }
    try:
        for topic in opdev.TOPICS:
            resolved = opdev.resolve_topic(store, topic.slug)
            assert resolved is not None
            matches = sum(len(ref["matches"]) for ref in resolved["references"])
            report["reference_count"] = int(report["reference_count"]) + len(topic.refs)
            report["topics"].append(
                {
                    "slug": topic.slug,
                    "references": len(topic.refs),
                    "matches": matches,
                    "missing": resolved["missing_references"],
                }
            )
            for label in resolved["missing_references"]:
                report["missing"].append({"topic": topic.slug, "reference": label})
    finally:
        store.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
