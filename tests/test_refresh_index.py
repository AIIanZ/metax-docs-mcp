from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
import httpx

from metax_docs_mcp.models import Document
from metax_docs_mcp.store import Store


def seed(path, identifier):
    store = Store(path)
    try:
        store.upsert(Document(id=identifier, source="official", title=identifier,
                              url="https://developer.metax-tech.com/doc", text="# Guide\nmcMalloc"))
    finally:
        store.close()


@pytest.fixture
def refresh(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/refresh_index.py"
    spec = importlib.util.spec_from_file_location("refresh_index", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = tmp_path / ".data"
    data.mkdir()
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples/sources.json").write_text('{"official": {}}')
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "DATA", data)
    monkeypatch.setattr(module, "LIVE", data / "index.sqlite3")
    seed(module.LIVE, "old")
    return module


@pytest.mark.parametrize("failure", ["partial", "verification"])
def test_failed_refresh_preserves_serving_snapshot(refresh, monkeypatch, failure):
    original = refresh.LIVE.read_bytes()

    def fake_command(command, output, errors):
        if "sync" in command:
            seed(Path(command[2]), "new")
            output.write_text(json.dumps({"failed": 0, "partial": failure == "partial"}))
        else:
            raise RuntimeError("MCP probe failed")

    monkeypatch.setattr(refresh, "run_logged", fake_command)
    assert refresh.refresh() == 1
    assert refresh.LIVE.read_bytes() == original
    assert not (refresh.DATA / "index.previous.sqlite3").exists()
    report = json.loads(next((refresh.DATA / "refresh").glob("*/result.json")).read_text())
    assert report["published"] is False


def test_verified_refresh_removes_stale_rows_and_keeps_backup(refresh, monkeypatch):
    original = refresh.LIVE.read_bytes()
    verified = []

    def fake_command(command, output, errors):
        if "sync" in command:
            seed(Path(command[2]), "new")
            output.write_text('{"failed": 0, "partial": false}')
        else:
            # Publication must happen only after the fresh snapshot passes.
            assert refresh.LIVE.read_bytes() == original
            store = Store(command[-1], readonly=True)
            try:
                assert store.get_document("new")
            finally:
                store.close()
            verified.append(True)
            output.write_text('{}')

    monkeypatch.setattr(refresh, "run_logged", fake_command)
    assert refresh.refresh() == 0
    assert verified
    with_backup = refresh.DATA / "index.previous.sqlite3"
    backup_store = Store(with_backup, readonly=True)
    try:
        assert backup_store.get_document("old")
        assert backup_store.get_document("new") is None
    finally:
        backup_store.close()
    store = Store(refresh.LIVE, readonly=True)
    try:
        assert store.get_document("new")
        assert store.get_document("old") is None
    finally:
        store.close()
    report = json.loads((refresh.DATA / "refresh-latest.json").read_text())
    assert report["published"] is True
    assert report["changes"] == {"added": 1, "removed": 1, "updated": 0}


def test_release_discovery_uses_numeric_stable_versions(refresh, monkeypatch, tmp_path):
    config = tmp_path / "versions.json"
    config.write_text(json.dumps({"github": {"repository_refs": {"MetaX-MACA/sglang": "release/v0.5.14"}}}))
    client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[
        {"name": "main"}, {"name": "release/v0.5.9"},
        {"name": "release/v0.5.14"}, {"name": "release/v0.5.20"},
        {"name": "release/v0.6.0rc1"},
    ]))
    monkeypatch.setattr(refresh.httpx, "Client", lambda **kwargs: client(transport=transport, **kwargs))
    result = refresh.current_config(config)
    assert result["github"]["repository_refs"]["MetaX-MACA/sglang"] == "release/v0.5.20"
    # The checked-in source selection remains unchanged; runs keep snapshots.
    assert json.loads(config.read_text())["github"]["repository_refs"]["MetaX-MACA/sglang"] == "release/v0.5.14"


def seed_all_sources(path):
    store = Store(path)
    try:
        store.upsert(Document(id="official:new", source="official", title="new",
                              url="https://developer.metax-tech.com/doc", text="# new"))
        store.upsert(Document(id="github:new", source="github", title="new",
                              url="https://github.com/MetaX-MACA/a/blob/abc123/README.md",
                              text="# new", repository="a", path="README.md", revision="abc123"))
    finally:
        store.close()


def test_rate_limited_github_batch_waits_then_publishes(refresh, monkeypatch, tmp_path):
    (tmp_path / "examples/sources.json").write_text(
        json.dumps({"official": {}, "github": {"repositories": ["a", "b"]}})
    )
    monkeypatch.setenv(refresh.GITHUB_BATCH_ENV, "5")
    waits: list[str] = []
    monkeypatch.setattr(refresh, "wait_for_github_reset", lambda label: waits.append(label))
    attempts = {"github": 0}

    def fake_command(command, output, errors):
        if "--config" not in command:
            output.write_text("{}")
            return 0
        config = json.loads(Path(command[command.index("--config") + 1]).read_text())
        if "github" in config:
            attempts["github"] += 1
            if attempts["github"] == 1:
                output.write_text(
                    json.dumps(
                        {
                            "failed": 1,
                            "partial": True,
                            "errors": [
                                {
                                    "url": "https://api.github.com/repos/MetaX-MACA/a",
                                    "error": "HTTP 403",
                                }
                            ],
                        }
                    )
                )
                return 1
        seed_all_sources(Path(command[2]))
        output.write_text(json.dumps({"failed": 0, "partial": False}))
        return 0

    monkeypatch.setattr(refresh, "run_logged", fake_command)
    assert refresh.refresh() == 0
    assert waits == ["github-01"]
    report = json.loads((refresh.DATA / "refresh-latest.json").read_text())
    assert report["published"] is True
    assert report["counts"] == {"official": 1, "github": 1}


def test_non_rate_limit_failure_is_not_retried(refresh, monkeypatch, tmp_path):
    (tmp_path / "examples/sources.json").write_text(json.dumps({"official": {}}))
    waits: list[str] = []
    monkeypatch.setattr(refresh, "wait_for_github_reset", lambda label: waits.append(label))

    def fake_command(command, output, errors):
        if "--config" in command:
            output.write_text(
                json.dumps(
                    {
                        "failed": 1,
                        "partial": True,
                        "errors": [{"url": "https://developer.metax-tech.com/x", "error": "HTTP 500"}],
                    }
                )
            )
            return 1
        output.write_text("{}")
        return 0

    monkeypatch.setattr(refresh, "run_logged", fake_command)
    assert refresh.refresh() == 1
    assert waits == []


@pytest.mark.parametrize("earlier_batches", [False, True])
@pytest.mark.parametrize("rate_failures", [1, 2])
def test_retry_publishes_only_the_latest_snapshot(
    refresh, monkeypatch, tmp_path, earlier_batches, rate_failures
):
    repositories = ["a", "b"]
    config = {"github": {"repositories": repositories}}
    if earlier_batches:
        repositories[:0] = ["prior", "another"]
        config["official"] = {}
    (tmp_path / "examples/sources.json").write_text(json.dumps(config))
    monkeypatch.setenv(refresh.GITHUB_BATCH_ENV, "2")
    waits = []
    monkeypatch.setattr(refresh, "wait_for_github_reset", waits.append)
    attempts = 0

    def put(db, repository, path, revision, text):
        store = Store(db)
        try:
            store.upsert(Document(
                id=f"{repository}/{path}", source="github", title=path,
                repository=f"MetaX-MACA/{repository}", path=path,
                revision=revision, text=text,
                url=f"https://github.com/MetaX-MACA/{repository}/blob/{revision}/{path}",
            ))
        finally:
            store.close()

    def fake_command(command, output, errors):
        nonlocal attempts
        db = Path(command[2])
        if "--config" not in command:
            # Verify the candidate actually exposed to the publication gate.
            db = Path(command[-1])
            with sqlite3.connect(db) as conn:
                assert conn.execute(
                    "SELECT path, revision FROM documents WHERE repository='MetaX-MACA/a'"
                ).fetchall() == [("keep.py", f"revision-{rate_failures + 1}")]
                assert conn.execute(
                    "SELECT count(*) FROM documents_fts WHERE document_id LIKE 'a/deleted-%'"
                ).fetchone()[0] == 0
                assert conn.execute(
                    "SELECT count(*) FROM sections WHERE document_id LIKE 'a/deleted-%'"
                ).fetchone()[0] == 0
            output.write_text("{}")
            return 0
        selected = json.loads(Path(command[-1]).read_text())
        if "official" in selected:
            seed(db, "official-prior")
        elif selected["github"]["repositories"] == ["prior", "another"]:
            for name in ("prior", "another"):
                put(db, name, "README.md", "prior-revision", "# Prior batch\nretained")
        else:
            attempts += 1
            revision = f"revision-{attempts}"
            put(db, "a", "keep.py", revision, "# Current\nretained")
            if attempts <= rate_failures:
                # A succeeds; B hits the limit. Before each retry the branch
                # advances, removing this attempt's file from A's next commit.
                put(db, "a", f"deleted-{attempts}.py", revision, "# Stale\nstale_snapshot")
                output.write_text(json.dumps({
                    "failed": 1, "partial": True,
                    "errors": [{"url": "https://api.github.com/repos/MetaX-MACA/b",
                                "error": "HTTP 403"}],
                }))
                return 1
            put(db, "b", "README.md", revision, "# Current\ncomplete")
        output.write_text('{"failed": 0, "partial": false}')
        return 0

    monkeypatch.setattr(refresh, "run_logged", fake_command)
    assert refresh.refresh() == 0
    assert attempts == rate_failures + 1
    assert len(waits) == rate_failures
    store = Store(refresh.LIVE, readonly=True)
    try:
        assert not store.search("stale_snapshot")
        assert store.get_document("a/keep.py")["revision"] == f"revision-{rate_failures + 1}"
        assert store.get_document("b/README.md")
        if earlier_batches:
            assert store.get_document("official-prior")
            for name in ("prior", "another"):
                doc = store.get_document(f"{name}/README.md")
                assert doc["revision"] == "prior-revision"
                assert store.get_section(doc["id"], "prior-batch")
    finally:
        store.close()
