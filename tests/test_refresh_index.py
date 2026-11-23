from __future__ import annotations

import importlib.util
import json
import os
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


def test_run_logged_uses_repository_source_for_child_processes(refresh, monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHONPATH", "/existing/python/path")
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return refresh.subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(refresh.subprocess, "run", fake_run)
    output = tmp_path / "stdout.json"
    errors = tmp_path / "stderr.log"

    assert refresh.run_logged(["child-command"], output, errors) == 0
    assert captured["env"]["PYTHONPATH"].split(os.pathsep) == [
        str(refresh.ROOT / "src"), "/existing/python/path"
    ]


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


def test_new_repository_refresh_skips_indexed_repositories(refresh, monkeypatch, tmp_path):
    store = Store(refresh.LIVE)
    store.upsert(Document(id='existing', source='github', title='existing', text='original',
                          repository='MetaX-MACA/already', url='https://github.com/MetaX-MACA/already'))
    store.close()
    (tmp_path/'examples/sources.json').write_text(json.dumps({'official': {}, 'github': {'repositories': ['ALREADY', 'new']}}))
    selections = []
    def run(command, output, errors):
        if '--config' in command:
            config = json.loads(Path(command[-1]).read_text())
            assert 'official' not in config
            selections.append(config['github']['repositories'])
            seed(Path(command[2]), 'added')
            output.write_text('{"failed":0,"partial":false}')
        else:
            output.write_text('{}')
        return 0
    monkeypatch.setattr(refresh, 'run_logged', run)
    assert refresh.refresh(new_repositories_only=True) == 0
    assert selections == [['new']]
    store = Store(refresh.LIVE, readonly=True)
    assert store.get_document('old') and store.get_document('existing') and store.get_document('added')
    store.close()


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


@pytest.mark.parametrize('change_config', [False, True])
def test_resume_reuses_complete_batches_and_discards_failed_attempt(refresh, monkeypatch, tmp_path, change_config):
    config = tmp_path/'examples/sources.json'
    config.write_text(json.dumps({'github':{'repositories':['a','b']}}))
    monkeypatch.setenv(refresh.GITHUB_BATCH_ENV,'1')
    calls = []
    def command(args, output, errors):
        if '--config' not in args:
            store = Store(args[-1],readonly=True)
            try:
                assert store.get_document('a')['revision'] == 'first-complete'
                assert store.get_document('stale') is None
                assert not store.search('stale_snapshot')
            finally:
                store.close()
            output.write_text('{}')
            return 0
        name = json.loads(Path(args[-1]).read_text())['github']['repositories'][0]
        calls.append(name)
        failing = name == 'b' and calls.count('b') == 1
        store = Store(args[2])
        try:
            store.upsert(Document(id='stale' if failing else name,source='github',title=name,
                text='# Stale\nstale_snapshot' if failing else '# Complete\nsource',
                repository=f'MetaX-MACA/{name}',revision='first-complete' if name=='a' else 'retry',
                url=f'https://github.com/MetaX-MACA/{name}'))
        finally:
            store.close()
        output.write_text(json.dumps({'failed':int(failing),'partial':failing,
            'errors':[{'url':'https://api.github.com/repos/MetaX-MACA/b','error':'HTTP 500'}] if failing else []}))
        return int(failing)
    monkeypatch.setattr(refresh,'run_logged',command)
    assert refresh.refresh() == 1
    directory = next((refresh.DATA/'refresh').iterdir())
    assert (directory/'resume.sqlite3').is_file()
    assert calls == ['a','b']
    if change_config:
        config.write_text(json.dumps({'github':{'repositories':['a','b','c']}}))
        assert refresh.refresh(resume_directory=directory) == 1
        assert calls == ['a','b']
        store = Store(refresh.LIVE,readonly=True)
        try:
            assert store.get_document('old')
        finally:
            store.close()
    else:
        assert refresh.refresh(resume_directory=directory) == 0
        assert calls == ['a','b','b']


def test_resume_after_verification_failure_keeps_complete_candidate(refresh,monkeypatch,tmp_path):
    (tmp_path/'examples/sources.json').write_text(json.dumps({'github':{'repositories':['a','b']}}))
    monkeypatch.setenv(refresh.GITHUB_BATCH_ENV,'1')
    calls=[]
    def command(args,output,errors):
        if '--config' in args:
            name=json.loads(Path(args[-1]).read_text())['github']['repositories'][0]
            calls.append(name);store=Store(args[2])
            try:store.upsert(Document(id=name,source='github',title=name,text='complete source',repository=f'MetaX-MACA/{name}',url=f'https://github.com/MetaX-MACA/{name}'))
            finally:store.close()
            output.write_text('{"failed":0,"partial":false}');return 0
        calls.append('verify');store=Store(args[-1],readonly=True)
        try:
            assert store.get_document('a') and store.get_document('b')
        finally:store.close()
        output.write_text('{}');return int(calls.count('verify')==1)
    monkeypatch.setattr(refresh,'run_logged',command)
    assert refresh.refresh()==1
    directory=next((refresh.DATA/'refresh').iterdir())
    # An older failed-batch checkpoint must not replace a completed crawl.
    seed(directory/'resume.sqlite3','obsolete-checkpoint')
    assert refresh.refresh(resume_directory=directory)==0
    assert calls==['a','b','verify','verify']
