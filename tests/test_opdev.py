from __future__ import annotations

import pytest

from metax_docs_mcp import opdev
from metax_docs_mcp.models import Document
from metax_docs_mcp.store import Store


def test_topic_map_is_internally_consistent():
    opdev.validate_topics()
    slugs = opdev.topic_slugs()
    assert len(slugs) == len(set(slugs))
    assert "overview" in slugs
    for topic in opdev.TOPICS:
        assert topic.steps, topic.slug
        assert topic.summary, topic.slug


def test_get_topic_accepts_slug_and_unique_keyword():
    assert opdev.get_topic("benchmark") is opdev.get_topic("benchmark")
    assert opdev.get_topic("C600").slug == "optimize-c600"
    # Ambiguous keywords must not silently pick a topic.
    assert opdev.get_topic("算子") is None
    assert opdev.get_topic("") is None
    assert opdev.get_topic("does-not-exist") is None


def test_catalogue_and_markdown_cover_every_topic():
    catalogue = opdev.catalogue()
    assert {entry["slug"] for entry in catalogue} == set(opdev.topic_slugs())
    rendered = opdev.render_markdown()
    for topic in opdev.TOPICS:
        assert topic.title in rendered
        assert f"`{topic.slug}`" in rendered


def test_resolve_topic_reports_missing_references(tmp_path):
    store = Store(tmp_path / "index.sqlite3")
    try:
        store.upsert(
            Document(
                id="github:mcoplib:bench",
                source="github",
                title="mxbench 性能测试工具",
                url="https://example.test/bench",
                text="# mxbench\n精度验证后测性能",
                repository="MetaX-MACA/mcoplib",
                path="benchmark/README.md",
                revision="abc123",
            )
        )
        resolved = opdev.resolve_topic(store, "benchmark")
        assert resolved is not None
        assert resolved["topic"]["slug"] == "benchmark"
        assert resolved["read_order"] == ["github:mcoplib:bench"]
        resolved_labels = {ref["label"]: ref["missing"] for ref in resolved["references"]}
        assert resolved_labels["mxbench 性能测试工具"] is False
        assert any(resolved_labels.values()), "unindexed references must be reported"
        assert set(resolved["missing_references"]) == {
            label for label, missing in resolved_labels.items() if missing
        }
        assert opdev.resolve_topic(store, "unknown-topic") is None
    finally:
        store.close()


def test_resolve_topic_rejects_out_of_range_limit(tmp_path):
    store = Store(tmp_path / "index.sqlite3")
    try:
        with pytest.raises(ValueError):
            opdev.resolve_topic(store, "benchmark", limit=0)
    finally:
        store.close()
