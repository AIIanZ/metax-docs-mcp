from __future__ import annotations

import sqlite3
import time

import pytest

from metax_docs_mcp.models import Document
from metax_docs_mcp.store import MAX_SEARCH_CANDIDATES, Store


def make_document(
    identifier: str = "official/guide",
    *,
    source: str = "official",
    title: str = "MXMACA guide",
    version: str = "v1",
    repository: str = "",
    text: str = "# Overview\nmx-smi 查看 GPU 拓扑\n## Setup\nCUDA_VISIBLE_DEVICES=0",
) -> Document:
    return Document(
        id=identifier,
        source=source,
        title=title,
        url=f"https://example.test/{identifier}",
        text=text,
        version=version,
        repository=repository,
        revision="abc123",
        metadata={"kind": "test"},
    )


def test_upsert_is_idempotent_but_refreshes_fetched_at(tmp_path):
    store = Store(tmp_path / "index.sqlite3")
    try:
        document = make_document()
        assert store.upsert(document) is True
        first = store.get_document(document.id)
        assert first is not None
        # Avoid relying on wall-clock granularity for the refresh assertion.
        time.sleep(0.001)
        assert store.upsert(document) is False
        second = store.get_document(document.id)
        assert second is not None
        assert second["fetched_at"] != first["fetched_at"]
        assert second["content_hash"] == first["content_hash"]
    finally:
        store.close()


def test_search_handles_technical_identifiers_and_chinese(tmp_path):
    store = Store(tmp_path / "index.sqlite3")
    try:
        store.upsert(make_document())
        store.upsert(
            make_document(
                "github/repo/file.py",
                source="github",
                title="runtime",
                version="main",
                repository="MetaX-MACA/runtime",
                text="## Runtime\nMCCL 初始化和设备映射。",
            )
        )

        mx_smi = store.search("mx-smi")
        assert [hit["id"] for hit in mx_smi] == ["official/guide"]
        assert "mx-smi" in mx_smi[0]["snippet"]

        cuda = store.search('"CUDA_VISIBLE_DEVICES"')
        assert [hit["id"] for hit in cuda] == ["official/guide"]

        chinese = store.search("拓扑")
        assert [hit["id"] for hit in chinese] == ["official/guide"]
        assert "拓扑" in chinese[0]["snippet"]

        assert store.search("MCCL", source="github", repository="MetaX-MACA/runtime")
        assert store.search("MCCL", source="official") == []
    finally:
        store.close()


def test_search_uses_fts_and_filters_before_candidate_bound(tmp_path):
    store = Store(tmp_path / "index.sqlite3")
    try:
        # Put more non-matching provenance rows than the bounded FTS candidate
        # count before the matching GitHub row.  Applying the provenance filter
        # after LIMIT would incorrectly return no result here.
        for index in range(MAX_SEARCH_CANDIDATES + 1):
            store.upsert(
                make_document(
                    f"official/{index}",
                    text="mx-smi 拓扑",
                )
            )
        store.upsert(
            make_document(
                "github/target.py",
                source="github",
                repository="MetaX-MACA/target",
                text="mx-smi 拓扑 设备",
            )
        )

        trace: list[str] = []
        store._conn.set_trace_callback(trace.append)  # type: ignore[attr-defined]
        technical = store.search(
            "mx-smi",
            source="github",
            repository="MetaX-MACA/target",
        )
        assert [hit["id"] for hit in technical] == ["github/target.py"]
        assert any("documents_fts MATCH" in statement for statement in trace)

        # Both Chinese terms are shorter than a trigram.  The SQL fallback
        # must apply every term before its bounded LIMIT, rather than returning
        # the first 5,000 partial matches for Python to inspect.
        chinese = store.search(
            "拓扑 设备",
            source="github",
            repository="MetaX-MACA/target",
        )
        assert [hit["id"] for hit in chinese] == ["github/target.py"]
    finally:
        store.close()


def test_sections_ignore_fenced_code_and_include_nested_content(tmp_path):
    store = Store(tmp_path / "index.sqlite3")
    try:
        text = (
            "# 运行\n"
            "```sh\n"
            "# shell comment\n"
            "mx-smi topo -m\n"
            "---\n"
            "```\n"
            "## 说明\n"
            "查看拓扑\n"
            "### 参数\n"
            "设备编号。\n"
            "# 下一节\n"
            "结束。\n"
        )
        document = make_document("doc", text=text)
        store.upsert(document)

        overview = store.get_section("doc", "运行")
        assert overview is not None
        assert "shell comment" in overview["text"]
        assert "查看拓扑" in overview["text"]
        assert "结束。" not in overview["text"]

        subsection = store.get_section("doc", "参数")
        assert subsection is not None
        assert subsection["text"] == "设备编号。"

        fetched = store.get_document("doc")
        assert fetched is not None
        assert [item["heading"] for item in fetched["sections"]] == ["运行", "说明", "参数", "下一节"]
    finally:
        store.close()


def test_document_and_section_reads_are_bounded(tmp_path):
    store = Store(tmp_path / "index.sqlite3")
    try:
        text = "# Long\n" + ("x" * 13_000)
        store.upsert(make_document("long", text=text))
        document = store.get_document("long", limit=20)
        assert document is not None
        assert len(document["text"]) == 20
        assert document["truncated"] is True
        assert document["next_offset"] == 20

        section = store.get_section("long", "Long")
        assert section is not None
        assert len(section["text"]) == 12_000
        assert section["truncated"] is True
        assert section["next_offset"] == section["offset"] + 12_000
    finally:
        store.close()


def test_list_sources_and_readonly_open(tmp_path):
    path = tmp_path / "index with spaces.sqlite3"
    writable = Store(path)
    writable.upsert(make_document())
    writable.upsert(
        make_document(
            "github/file.py",
            source="github",
            repository="MetaX-MACA/demo",
            version="main",
        )
    )
    writable.close()

    readonly = Store(path, readonly=True)
    try:
        sources = readonly.list_sources()
        assert sources["total_documents"] == 2
        assert sources["by_source"] == {"official": 1, "github": 1, "docker": 0}
        with pytest.raises(sqlite3.OperationalError):
            readonly.upsert(make_document("new"))
    finally:
        readonly.close()


def test_invalid_inputs_are_rejected(tmp_path):
    store = Store(tmp_path / "index.sqlite3")
    try:
        with pytest.raises(ValueError):
            store.upsert(make_document("", text="x"))
        with pytest.raises(ValueError):
            store.search("   ")
        with pytest.raises(ValueError):
            store.get_document("missing", offset=-1)
        with pytest.raises(ValueError):
            store.get_section("missing", "")
    finally:
        store.close()


def test_docker_records_are_searchable_with_source_filter(tmp_path):
    store = Store(tmp_path / 'images.sqlite3')
    store.upsert(make_document('docker/image', source='docker', title='MXMACA C500', text='docker pull registry.example/maca:3.8'))
    assert store.search('docker', source='docker')[0]['id'] == 'docker/image'
    assert store.list_sources()['docker'] == 1
    assert store.list_sources()['sources']['docker']['newest_fetched_at']
    store.close()


def test_mixed_short_chinese_terms_filter_before_fts_candidate_cap(tmp_path, monkeypatch):
    import metax_docs_mcp.store as module
    monkeypatch.setattr(module, 'MAX_SEARCH_CANDIDATES', 3)
    store = Store(tmp_path / 'mixed.sqlite3')
    for i in range(6):
        store.upsert(make_document(str(i), title='mx-smi', text='mx-smi '*20))
    store.upsert(make_document('target', title='target', text='mx-smi 查看拓扑'))
    assert store.search('mx-smi 拓扑')[0]['id'] == 'target'
    store.close()
