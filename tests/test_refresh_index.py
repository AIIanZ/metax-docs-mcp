from __future__ import annotations

import importlib.util
import json
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
