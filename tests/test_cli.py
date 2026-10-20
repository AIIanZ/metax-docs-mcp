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

    def find_documents(self, *args, **kwargs):
        self.calls.append(("find_documents", args, kwargs))
        return []

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


def test_opdev_lists_topics_and_resolves_one(monkeypatch, capsys, tmp_path):
    fake = FakeStore()
    monkeypatch.setattr(cli, "_open_readonly_store", lambda path: fake)

    status = cli.main(["--db", str(tmp_path / "index.sqlite3"), "opdev"])
    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    assert {entry["slug"] for entry in payload["topics"]} >= {"benchmark", "optimize-c600"}
    # Listing topics must not touch the index; only closing the store is recorded.
    assert [call[0] for call in fake.calls] == ["close"]

    status = cli.main(["--db", str(tmp_path / "index.sqlite3"), "opdev", "benchmark", "--limit", "5"])
    assert status == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["topic"]["slug"] == "benchmark"
    lookups = [call for call in fake.calls if call[0] == "find_documents"]
    assert lookups and all(call[2]["limit"] == 5 for call in lookups)


def test_opdev_unknown_topic_is_a_cli_error(monkeypatch, capsys, tmp_path):
    fake = FakeStore()
    monkeypatch.setattr(cli, "_open_readonly_store", lambda path: fake)

    status = cli.main(["--db", str(tmp_path / "index.sqlite3"), "opdev", "nope"])
    assert status == 1
    assert "unknown operator topic" in capsys.readouterr().err
