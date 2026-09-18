from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from metax_docs_mcp.models import Document
from metax_docs_mcp.store import Store


def _seed_database(path: Path) -> None:
    store = Store(path)
    try:
        store.upsert(
            Document(
                id="github:maca-samples:README.md@abc123",
                source="github",
                title="maca-samples README",
                url="https://github.com/MetaX-MACA/maca-samples/blob/abc123/README.md",
                text="# mx-smi\nUse mx-smi to inspect the GPU.\n",
                repository="maca-samples",
                path="README.md",
                revision="abc123",
                metadata={"fixture": True},
            )
        )
    finally:
        store.close()


def test_stdio_initialize_list_tools_and_call_tool(tmp_path):
    """Exercise the protocol through the official SDK client, not functions."""

    asyncio.run(_stdio_round_trip(tmp_path))


async def _stdio_round_trip(tmp_path):
    db = tmp_path / "index.sqlite3"
    _seed_database(db)
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    env = os.environ.copy()
    env["PYTHONPATH"] = source_root + os.pathsep + env.get("PYTHONPATH", "")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "metax_docs_mcp", "--db", str(db), "serve"],
        env=env,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "metax-docs-mcp"

            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            assert names == {
                "search_metax_docs",
                "get_metax_document",
                "get_metax_section",
                "list_metax_sources",
            }
            search_tool = next(
                tool for tool in listed.tools if tool.name == "search_metax_docs"
            )
            annotations = search_tool.annotations
            assert annotations is not None
            assert getattr(annotations, "readOnlyHint", True) is True
            assert "untrusted" in (search_tool.description or "").lower()

            result = await session.call_tool(
                "search_metax_docs", {"query": "mx-smi", "limit": 3}
            )
            assert not result.isError
            payload = getattr(result, "structuredContent", None)
            if not payload:
                payload = json.loads(result.content[0].text)
            # FastMCP uses a structured ``result`` envelope for list returns.
            rendered = json.dumps(payload, ensure_ascii=False)
            assert "mx-smi" in rendered

            document = await session.call_tool(
                "get_metax_document",
                {"document_id": "github:maca-samples:README.md@abc123"},
            )
            assert not document.isError
            assert "mx-smi" in json.dumps(
                getattr(document, "structuredContent", None), ensure_ascii=False
            )

            section = await session.call_tool(
                "get_metax_section",
                {
                    "document_id": "github:maca-samples:README.md@abc123",
                    "section": "mx-smi",
                },
            )
            assert not section.isError
            assert "inspect the GPU" in json.dumps(
                getattr(section, "structuredContent", None), ensure_ascii=False
            )

            sources = await session.call_tool("list_metax_sources", {})
            assert not sources.isError
            assert "maca-samples" in json.dumps(
                getattr(sources, "structuredContent", None), ensure_ascii=False
            )

            invalid = await session.call_tool(
                "search_metax_docs", {"query": "", "limit": 3}
            )
            assert invalid.isError


def test_stdio_missing_database_is_error_and_does_not_create_file(tmp_path):
    asyncio.run(_missing_database_round_trip(tmp_path))


async def _missing_database_round_trip(tmp_path):
    db = tmp_path / "does-not-exist.sqlite3"
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    env = os.environ.copy()
    env["PYTHONPATH"] = source_root + os.pathsep + env.get("PYTHONPATH", "")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "metax_docs_mcp", "--db", str(db), "serve"],
        env=env,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("list_metax_sources", {})
            assert result.isError
    assert not db.exists()
