from __future__ import annotations

import json

from metax_docs_mcp import cli


class FakeStore:
    def __init__(self, result=None):
        self.result = result
        self.calls: list[tuple[str, tuple, dict]] = []

    def search(self, *args, **kwargs):
        self.calls.append(("search", args, kwargs))
        return self.result or [{"id": "doc-1", "title": "mx-smi"}]

    def get_document(self, *args, **kwargs):
        self.calls.append(("get_document", args, kwargs))
        return self.result or {"id": "doc-1", "text": "body"}

    def get_section(self, *args, **kwargs):
        self.calls.append(("get_section", args, kwargs))
        return self.result or {"id": "doc-1", "section": "intro"}

    def list_sources(self, *args, **kwargs):
        self.calls.append(("list_sources", args, kwargs))
        return self.result or {"official": {"documents": 1}}

    def close(self):
        self.calls.append(("close", (), {}))


def test_search_emits_json_and_forwards_filters(monkeypatch, capsys, tmp_path):
    fake = FakeStore()
    monkeypatch.setattr(cli, "_open_readonly_store", lambda path: fake)

    status = cli.main(
        [
            "--db",
            str(tmp_path / "index.sqlite3"),
            "search",
            "mx-smi",
            "--source",
            "github",
            "--repository",
            "maca-samples",
            "--version",
            "main",
            "--limit",
            "3",
        ]
    )

    assert status == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == "doc-1"
    assert fake.calls[0] == (
        "search",
        ("mx-smi",),
        {
            "source": "github",
            "repository": "maca-samples",
            "version": "main",
            "limit": 3,
        },
    )


def test_sync_failed_count_is_nonzero_and_still_emits_report(monkeypatch, capsys, tmp_path):
    fake = FakeStore()
    monkeypatch.setattr(cli, "_open_store", lambda path: fake)
    monkeypatch.setattr(
        "metax_docs_mcp.sources.sync_sources",
        lambda store, config, source="all": {
            "discovered": 2,
            "indexed": 1,
            "unchanged": 0,
            "failed": 1,
            "errors": ["one source failed"],
        },
    )

    config = tmp_path / "sources.json"
    config.write_text("{}", encoding="utf-8")
    status = cli.main(
        ["--db", str(tmp_path / "index.sqlite3"), "sync", "--config", str(config)]
    )

    captured = capsys.readouterr()
    assert status == cli.SYNC_FAILURE_EXIT
    assert json.loads(captured.out)["failed"] == 1
    assert "failed item" in captured.err


def test_sync_partial_has_distinct_status(monkeypatch, capsys, tmp_path):
    fake = FakeStore()
    monkeypatch.setattr(cli, "_open_store", lambda path: fake)
    monkeypatch.setattr(
        "metax_docs_mcp.sources.sync_sources",
        lambda store, config, source="all": {
            "discovered": 100,
            "indexed": 100,
            "unchanged": 0,
            "failed": 0,
            "errors": [],
            "truncated": True,
        },
    )

    config = tmp_path / "sources.json"
    config.write_text("{}", encoding="utf-8")
    status = cli.main(
        ["--db", str(tmp_path / "index.sqlite3"), "sync", "--config", str(config)]
    )

    assert status == cli.SYNC_PARTIAL_EXIT
    assert json.loads(capsys.readouterr().out)["truncated"] is True


def test_missing_document_is_a_cli_error(monkeypatch, capsys, tmp_path):
    fake = FakeStore(result=None)
    fake.get_document = lambda *args, **kwargs: None
    monkeypatch.setattr(cli, "_open_readonly_store", lambda path: fake)

    status = cli.main(
        ["--db", str(tmp_path / "index.sqlite3"), "get", "missing"]
    )

    assert status == 1
    assert "document not found" in capsys.readouterr().err
