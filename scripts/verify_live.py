"""Read-only acceptance probe against an already synchronized local index.

Usage: .venv/bin/python scripts/verify_live.py .data/index.sqlite3
No networking or source writes; prints machine-readable evidence.
"""
import json
import sys
from metax_docs_mcp.store import Store

store = Store(sys.argv[1])
report = {"sources": store.list_sources(), "queries": []}
for query, source in [("mx-smi", "official"), ("maca", "github"), ("MXMACA", "docker")]:
    hits = store.search(query, source=source, limit=3)
    assert hits, f"No hits for {source}: {query}"
    hit = hits[0]
    document = store.get_document(hit["id"], limit=12000)
    assert document and document["source"] == source
    assert document["url"].startswith("https://")
    if source == "github":
        assert len(document["revision"]) == 40
        assert document["revision"] in document["url"]
    if source == "docker":
        assert document["metadata"]["package_kind"] == "MXMACA"
        assert document["metadata"]["chip_name"]
        assert document["metadata"]["pull_command_status"] in {
            "source_record", "inferred_from_path", "not_exposed_by_public_list"
        }
    report["queries"].append({"query": query, "source": source, "hit": hit,
                              "read_keys": list(document), "read_ok": True})
store.close()
print(json.dumps(report, ensure_ascii=False, indent=2))
