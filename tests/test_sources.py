"""Offline contract tests for the bounded dual-source ingester."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

import pytest

from metax_docs_mcp import sources


class FakeResponse:
    def __init__(self, status_code: int = 200, content: bytes = b"", headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    def iter_bytes(self):
        yield self.content


class _ResponseContext:
    def __init__(self, response: FakeResponse):
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class FakeClient:
    def __init__(self, routes: dict[str, Any]):
        self.routes = routes
        self.calls: list[str] = []

    def stream(self, method: str, url: str):
        assert method == "GET"
        self.calls.append(url)
        route = self.routes.get(url)
        if route is None:
            route = next((value for key, value in self.routes.items() if key.endswith("*") and url.startswith(key[:-1])), None)
        if route is None:
            raise AssertionError(f"unexpected request: {url}")
        value = route() if callable(route) else route
        if isinstance(value, BaseException):
            raise value
        return _ResponseContext(value)

    def close(self):
        return None


@dataclass
class MemoryStore:
    documents: dict[str, Any]

    def __init__(self):
        self.documents = {}

    def upsert(self, document):
        old = self.documents.get(document.id)
        new_hash = document.metadata.get("content_sha256")
        if old is not None and old.metadata.get("content_sha256") == new_hash:
            self.documents[document.id] = document
            return False
        self.documents[document.id] = document
        return True


def _html(title: str, body: str, links: list[str] | None = None) -> bytes:
    anchors = "".join(f'<a href="{link}">{link}</a>' for link in (links or []))
    return (
        f"<html><head><title>{title}</title></head><body>"
        f'<div class="document"><h1>{title}</h1><p>{body}</p>{anchors}'
        "<pre><code>mx-smi --query</code></pre></div></body></html>"
    ).encode()


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: FakeClient) -> None:
    monkeypatch.setattr(sources, "_new_client", lambda timeout, headers: client)


def test_official_directory_catalog_and_child_version_metadata(monkeypatch: pytest.MonkeyPatch):
    catalog_url = "https://developer.metax-tech.com/api/client/document/series/filter/?page=1&page_size=100"
    index_url = "https://developer.metax-tech.com/api/client/document/preview/1484/index.html"
    child_url = "https://developer.metax-tech.com/api/client/document/preview/1484/split_files/intro.html"
    catalog = {
        "code": 0,
        "data": [
            {
                "file_id": 1484,
                "preview_file_id": 1484,
                "series_name": "mx-smi",
                "version": "3.8.2.x",
                "latest_version": "3.8.3.x",
                "versions": [{"version": "old-version", "preview_file_id": 7}],
            }
        ],
    }
    client = FakeClient(
        {
            catalog_url: FakeResponse(200, json.dumps(catalog).encode(), {"content-type": "application/json"}),
            index_url: FakeResponse(
                200,
                _html("mx-smi", "Official body " * 20, ["split_files/intro.html"]),
                {"content-type": "text/html"},
            ),
            child_url: FakeResponse(200, _html("Introduction", "Child body " * 20), {"content-type": "text/html"}),
        }
    )
    _patch_client(monkeypatch, client)
    store = MemoryStore()
    report = sources.sync_sources(
        store,
        {
            "official": {"seeds": ["https://developer.metax-tech.com/doc"], "max_pages": 10, "discover": True},
            "http": {"retries": 0, "rate_limit": 0},
        },
        source="official",
    )

    assert report["failed"] == 0
    assert report["indexed"] == 2
    assert len(store.documents) == 2
    assert {document.version for document in store.documents.values()} == {"3.8.2.x"}
    assert {document.metadata["version"] for document in store.documents.values()} == {"3.8.2.x"}
    assert {document.metadata["latest_version"] for document in store.documents.values()} == {"3.8.3.x"}
    assert all("mx-smi" in document.text or "Child body" in document.text for document in store.documents.values())
    assert all(document.metadata["content_sha256"] for document in store.documents.values())
    # The directory shell itself is never indexed or fetched as a document.
    assert "https://developer.metax-tech.com/doc" not in client.calls


def test_github_uses_fixed_commit_and_hash_idempotency(monkeypatch: pytest.MonkeyPatch):
    root = "https://api.github.com/repos/MetaX-MACA/maca-samples"
    revision = "a" * 40
    readme_sha = "b" * 40
    code_sha = "c" * 40
    readme = "# MACA samples\n\n" + ("README body " * 20)
    routes = {
        root: FakeResponse(200, json.dumps({"full_name": "MetaX-MACA/maca-samples", "default_branch": "main", "private": False}).encode()),
        root + "/commits/main": FakeResponse(200, json.dumps({"sha": revision}).encode()),
        root + f"/git/trees/{revision}?recursive=1": FakeResponse(
            200,
            json.dumps(
                {
                    "truncated": False,
                    "tree": [
                        {"path": "README.md", "type": "blob", "sha": readme_sha, "size": len(readme)},
                        {"path": "src/sample.py", "type": "blob", "sha": code_sha, "size": 20},
                        {"path": "assets/logo.png", "type": "blob", "sha": "d" * 40, "size": 12},
                    ],
                }
            ).encode(),
        ),
        root + f"/git/blobs/{readme_sha}": FakeResponse(
            200,
            json.dumps({"encoding": "base64", "content": base64.b64encode(readme.encode()).decode()}).encode(),
        ),
        root + f"/git/blobs/{code_sha}": FakeResponse(
            200,
            json.dumps({"encoding": "base64", "content": base64.b64encode(b"print('maca')\n").decode()}).encode(),
        ),
    }
    first_client = FakeClient(routes)
    _patch_client(monkeypatch, first_client)
    store = MemoryStore()
    config = {
        "github": {"organization": "MetaX-MACA", "repositories": ["maca-samples"], "max_files_per_repo": 20, "include_code": False},
        "http": {"retries": 0, "rate_limit": 0},
    }
    first = sources.sync_sources(store, config, source="github")
    assert first["failed"] == 0
    assert first["indexed"] == 1
    document = next(iter(store.documents.values()))
    assert document.revision == revision
    assert revision in document.url
    assert document.path == "README.md"
    stable_id = document.id

    second_client = FakeClient(routes)
    _patch_client(monkeypatch, second_client)
    second = sources.sync_sources(store, config, source="github")
    assert second["failed"] == 0
    assert second["indexed"] == 0
    assert second["unchanged"] == 1
    assert next(iter(store.documents.values())).id == stable_id
    assert not any("sample.py" in path for path in second_client.calls)


def test_github_tree_truncation_and_one_blob_failure_are_visible(monkeypatch: pytest.MonkeyPatch):
    root = "https://api.github.com/repos/MetaX-MACA/maca-samples"
    revision = "e" * 40
    good_sha = "f" * 40
    bad_sha = "1" * 40
    routes = {
        root: FakeResponse(200, json.dumps({"default_branch": "main", "private": False}).encode()),
        root + "/commits/main": FakeResponse(200, json.dumps({"sha": revision}).encode()),
        root + f"/git/trees/{revision}?recursive=1": FakeResponse(
            200,
            json.dumps(
                {
                    "truncated": True,
                    "tree": [
                        {"path": "README.md", "type": "blob", "sha": good_sha, "size": 4},
                        {"path": "docs/failure.rst", "type": "blob", "sha": bad_sha, "size": 4},
                    ],
                }
            ).encode(),
        ),
        root + f"/git/blobs/{good_sha}": FakeResponse(
            200,
            json.dumps({"encoding": "base64", "content": base64.b64encode(b"good docs").decode()}).encode(),
        ),
        root + f"/git/blobs/{bad_sha}": RuntimeError("temporary blob failure"),
    }
    client = FakeClient(routes)
    _patch_client(monkeypatch, client)
    report = sources.sync_sources(
        MemoryStore(),
        {"github": {"repositories": ["maca-samples"], "max_files_per_repo": 20}, "http": {"retries": 0, "rate_limit": 0}},
        source="github",
    )
    assert report["indexed"] == 1
    assert report["failed"] == 1
    assert report["partial"] is True
    assert report["truncated"] is True
    assert any("failure.rst" in error.get("path", "") for error in report["errors"])


def test_http200_shell_and_redirect_escape_are_rejected(monkeypatch: pytest.MonkeyPatch):
    shell_url = "https://developer.metax-tech.com/doc"
    redirect_url = "https://developer.metax-tech.com/api/client/document/preview/1/index.html"
    client = FakeClient(
        {
            shell_url: FakeResponse(200, b"<html><head><title>Docs</title></head><body><script>app()</script></body></html>", {"content-type": "text/html"}),
            redirect_url: FakeResponse(302, b"", {"location": "https://evil.example.test/steal"}),
        }
    )
    _patch_client(monkeypatch, client)
    shell_report = sources.sync_sources(
        MemoryStore(),
        {"official": {"seeds": [shell_url], "discover": False, "max_pages": 1}, "http": {"retries": 0, "rate_limit": 0}},
        source="official",
    )
    assert shell_report["indexed"] == 0
    assert shell_report["failed"] == 1
    redirect_report = sources.sync_sources(
        MemoryStore(),
        {"official": {"seeds": [redirect_url], "discover": False, "max_pages": 1}, "http": {"retries": 0, "rate_limit": 0}},
        source="official",
    )
    assert redirect_report["failed"] == 1
    assert "evil.example.test" not in json.dumps(redirect_report)



def test_html_preserves_literal_code_and_removes_permalink():
    page = sources._parse_html(b'<html><main><h1>Run<a class="headerlink" href="#run">#</a></h1><pre>export CUDA_VISIBLE_DEVICES=0\n# comment\n</pre></main></html>', max_document_chars=10000)
    assert 'CUDA_VISIBLE_DEVICES=0' in page.text
    assert '\\_' not in page.text
    assert '# Run\n' in page.text


def test_manual_crawl_does_not_follow_other_versions():
    base = 'https://developer.metax-tech.com/api/client/document/preview/1484/index.html'
    page = sources._ParsedPage('x', 'x', ('split_files/test.html', '../1485/index.html', '/doc'))
    assert list(sources._page_links(page, base, {sources.OFFICIAL_HOST})) == [base.replace('index.html', 'split_files/test.html')]


def test_catalog_paginates_using_total_count():
    root = 'https://developer.metax-tech.com/api/client/document/series/filter/'
    routes = {}
    for page in (1, 2):
        entries = [{'file_id': n, 'version':'1'} for n in range((page-1)*100 + 1, (page-1)*100 + 1 + (100 if page == 1 else 1))]
        routes[f'{root}?page={page}&page_size=100'] = FakeResponse(content=json.dumps({'code':0,'data':entries,'page_info':{'total_count':101}}).encode())
    fetcher = sources._HttpFetcher(FakeClient(routes), allowed_hosts={sources.OFFICIAL_HOST}, retries=0, max_response_bytes=1000000, rate_limiter=sources._RateLimiter(0))
    entries, reasons = sources._official_catalog_entries(fetcher)
    assert len(entries) == 101
    assert not reasons


def test_html_truncation_is_reported(monkeypatch):
    url = 'https://developer.metax-tech.com/api/client/document/preview/1/index.html'
    _patch_client(monkeypatch, FakeClient({url: FakeResponse(content=_html('Long', 'x'*3000),headers={'content-type':'text/html'})}))
    report = sources.sync_sources(MemoryStore(), {'official':{'seeds':[url],'discover':False},'http':{'max_document_chars':1024,'rate_limit':0}},source='official')
    assert report['indexed'] == 1
    assert report['truncated'] and report['partial']
