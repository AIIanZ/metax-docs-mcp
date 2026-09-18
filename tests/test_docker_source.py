"""Offline contract tests for the public SoftNova Docker source."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from metax_docs_mcp import docker_source


@dataclass
class FakeResponse:
    payload: Any
    status_code: int = 200
    content_type: str = "application/json"
    truncated: bool = False

    @property
    def headers(self) -> dict[str, str]:
        return {"content-type": self.content_type}

    @property
    def content(self) -> bytes:
        if isinstance(self.payload, (bytes, bytearray)):
            return bytes(self.payload)
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class FakeFetcher:
    def __init__(self, routes: dict[int, Any]):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url: str) -> FakeResponse:
        self.calls.append(url)
        page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
        route = self.routes.get(page)
        if isinstance(route, BaseException):
            raise route
        if route is None:
            raise AssertionError(f"unexpected page request: {url}")
        return route if isinstance(route, FakeResponse) else FakeResponse(route)


class MemoryStore:
    def __init__(self):
        self.documents: dict[str, Any] = {}

    def upsert(self, document):
        old = self.documents.get(document.id)
        changed = old is None or old.metadata.get("content_sha256") != document.metadata.get("content_sha256")
        self.documents[document.id] = document
        return changed


def _stats() -> dict[str, Any]:
    return {
        "source": "docker",
        "discovered": 0,
        "indexed": 0,
        "unchanged": 0,
        "failed": 0,
        "errors": [],
        "truncated": False,
        "partial": False,
        "truncation_reasons": [],
    }


def _payload(results: list[dict[str, Any]], total: int, total_page: int) -> dict[str, Any]:
    return {"code": 0, "message": "", "error": "", "data": {"results": results, "total": total, "total_page": total_page, "has_preview": False}}


def _item(record_id: str, *, package_kind: str = "AI") -> dict[str, Any]:
    return {
        "_id": record_id,
        "ai_frame": "vllm-metax",
        "ai_frame_version": "0.8.5",
        "arch": "amd64",
        "chip_name": ["曦云C500系列", "曦云C600系列"],
        "compatible_maca": "3.8.2.x",
        "created_at": "2026-09-18 12:00:00",
        "deliver_type": "分层包",
        "dimension": "docker",
        "file_type": "docker",
        "maca_main_version": "3.8.2.x",
        "package_kind": package_kind,
        "package_name": f"vllm-metax:0.8.5-{record_id}",
        "path": f"public-ai-release/maca/vllm-metax:0.8.5-{record_id}",
        "pull_type": "docker",
        "python_version": "3.11",
        "pytorch_version": "2.6",
        "updated_at": "2026-09-18 12:00:00",
    }


def test_sync_docker_reads_public_v3_pages_and_preserves_image_metadata():
    first = _item("one")
    second = _item("two")
    second.update({"registry": "registry.example.test", "pull_cmd": "docker pull registry.example.test/team/two:latest"})
    third = _item("three", package_kind="MXMACA")
    fetcher = FakeFetcher(
        {
            1: _payload([first, second], total=3, total_page=2),
            2: _payload([third], total=3, total_page=2),
        }
    )
    store = MemoryStore()
    stats = _stats()
    docker_source.sync_docker(
        store,
        {
            "seeds": [
                "https://developer.metax-tech.com/softnova/docker?chip_name=%E6%9B%A6%E4%BA%91C500%E7%B3%BB%E5%88%97&package_kind=AI&dimension=docker&deliver_type=%E5%88%86%E5%B1%82%E5%8C%85"
            ],
            "page_size": 2,
        },
        fetcher,
        stats,
        max_document_chars=2_000,
    )

    assert len(store.documents) == 3
    assert stats["discovered"] == 3
    assert stats["indexed"] == 3
    assert stats["failed"] == 0
    assert stats["partial"] is False
    assert len(fetcher.calls) == 2
    assert all("/softnova/api/v3/dlhub/docker_package_info/" in call for call in fetcher.calls)
    assert all("page=" in call and "size=2" in call for call in fetcher.calls)

    explicit = next(document for document in store.documents.values() if document.metadata["source_record_id"] == "two")
    assert explicit.source == "docker"
    assert explicit.metadata["registry"] == "registry.example.test"
    assert explicit.metadata["pull_command"] == "docker pull registry.example.test/team/two:latest"
    assert explicit.metadata["pull_command_status"] == "source_record"
    assert explicit.metadata["framework"] == "vllm-metax"
    assert explicit.metadata["framework_version"] == "0.8.5"
    assert explicit.metadata["maca_version"] == "3.8.2.x"
    assert "曦云C500系列" in explicit.text
    assert "docker pull" in explicit.text
    assert explicit.path.startswith("public-ai-release/")

    inferred = next(document for document in store.documents.values() if document.metadata["source_record_id"] == "one")
    assert inferred.metadata["registry"] == ""
    assert inferred.metadata["pull_command"].startswith("docker pull public-ai-release/")
    assert inferred.metadata["pull_command_status"] == "inferred_from_path"


def test_source_record_id_is_not_assumed_unique():
    ubuntu = _item("shared")
    rocky = _item("shared")
    rocky["package_name"] = "vllm-metax:0.8.5-rocky"
    rocky["path"] = "public-ai-release/maca/vllm-metax:0.8.5-rocky"
    store = MemoryStore()
    stats = _stats()
    docker_source.sync_docker(
        store,
        {"seeds": [docker_source.DOCKER_PAGE_URL], "page_size": 10},
        FakeFetcher({1: _payload([ubuntu, rocky], total=2, total_page=1)}),
        stats,
        max_document_chars=2_000,
    )
    assert len(store.documents) == 2
    assert stats["indexed"] == 2


def test_sync_docker_is_idempotent_for_same_record_hash():
    item = _item("same")
    routes = {1: _payload([item], total=1, total_page=1)}
    store = MemoryStore()
    first_fetcher = FakeFetcher(routes)
    first_stats = _stats()
    config = {"seeds": [docker_source.DOCKER_API_URL], "page_size": 10}
    docker_source.sync_docker(store, config, first_fetcher, first_stats, max_document_chars=2_000)
    second_fetcher = FakeFetcher(routes)
    second_stats = _stats()
    docker_source.sync_docker(store, config, second_fetcher, second_stats, max_document_chars=2_000)

    assert first_stats["indexed"] == 1
    assert second_stats["indexed"] == 0
    assert second_stats["unchanged"] == 1
    assert second_stats["failed"] == 0
    assert second_stats["truncated"] is False


def test_sync_docker_exposes_http_schema_and_row_failures():
    fetcher = FakeFetcher({1: FakeResponse({"code": -1, "message": "筛选参数错误", "data": None})})
    stats = _stats()
    store = MemoryStore()
    docker_source.sync_docker(
        store,
        {"seeds": [docker_source.DOCKER_PAGE_URL]},
        fetcher,
        stats,
        max_document_chars=2_000,
    )
    assert stats["failed"] == 1
    assert stats["partial"] is True
    assert "筛选参数错误" in stats["errors"][0]["error"]
    assert not store.documents

    bad_fetcher = FakeFetcher({1: _payload([{"_id": "bad"}], total=1, total_page=1)})
    bad_stats = _stats()
    docker_source.sync_docker(store, {"seeds": [docker_source.DOCKER_PAGE_URL]}, bad_fetcher, bad_stats, max_document_chars=2_000)
    assert bad_stats["discovered"] == 1
    assert bad_stats["failed"] == 1
    assert bad_stats["partial"] is True
    assert bad_stats["errors"][0]["path"] == "bad"


def test_sync_docker_marks_page_bound_but_does_not_mark_exact_last_page_partial():
    item = _item("one")
    complete = _stats()
    docker_source.sync_docker(
        MemoryStore(),
        {"seeds": [docker_source.DOCKER_PAGE_URL], "page_size": 1, "max_pages": 1},
        FakeFetcher({1: _payload([item], total=1, total_page=1)}),
        complete,
        max_document_chars=2_000,
    )
    assert complete["truncated"] is False

    partial = _stats()
    docker_source.sync_docker(
        MemoryStore(),
        {"seeds": [docker_source.DOCKER_PAGE_URL], "page_size": 1, "max_pages": 1},
        FakeFetcher({1: _payload([item], total=2, total_page=2)}),
        partial,
        max_document_chars=2_000,
    )
    assert partial["truncated"] is True
    assert any("page limit" in reason for reason in partial["truncation_reasons"])


def test_overlapping_pages_are_reported_as_incomplete():
    same = _item("same")
    stats = _stats()
    docker_source.sync_docker(
        MemoryStore(),
        {"seeds": [docker_source.DOCKER_PAGE_URL], "page_size": 1},
        FakeFetcher(
            {
                1: _payload([same], total=2, total_page=2),
                2: _payload([same], total=2, total_page=2),
            }
        ),
        stats,
        max_document_chars=2_000,
    )
    assert stats["discovered"] == 2
    assert stats["partial"] and stats["truncated"]
    assert any("1 unique records of 2" in reason for reason in stats["truncation_reasons"])
