"""Command line interface for the local MetaX documentation index."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence


DEFAULT_DB_ENV = "METAX_DOCS_DB"
DEFAULT_DB_PATH = "~/.cache/metax-docs-mcp/index.sqlite3"
SYNC_FAILURE_EXIT = 1
SYNC_PARTIAL_EXIT = 2


def default_db_path() -> Path:
    """Return the configured local SQLite path without creating it."""

    configured = os.environ.get(DEFAULT_DB_ENV, DEFAULT_DB_PATH)
    return Path(configured).expanduser()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="metax-docs-mcp",
        description="Search a local index of MetaX official docs, GitHub, and Docker images.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"SQLite index path (default: ${DEFAULT_DB_ENV} or {DEFAULT_DB_PATH})",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    sync = commands.add_parser("sync", help="fetch and index configured sources")
    sync.add_argument("--config", required=True, type=Path, metavar="PATH")
    sync.add_argument(
        "--source",
        choices=("official", "github", "docker", "all"),
        default="all",
        help="source to synchronize (a bounded/partial sync exits 2)",
    )

    search = commands.add_parser("search", help="search indexed documents")
    search.add_argument("query")
    _add_search_filters(search)

    get = commands.add_parser("get", help="read a document by stable id")
    get.add_argument("document_id", metavar="ID")
    get.add_argument("--offset", type=_nonnegative_int, default=0)
    get.add_argument("--limit", type=_positive_int, default=12000)

    section = commands.add_parser("section", help="read a document section")
    section.add_argument("document_id", metavar="ID")
    section.add_argument("section")

    commands.add_parser("sources", help="show indexed source counts")

    serve = commands.add_parser("serve", help="run the MCP server over stdio")
    serve.set_defaults(command="serve")

    return parser


def _add_search_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", choices=("official", "github", "docker"), default=None)
    parser.add_argument("--repository", default=None)
    parser.add_argument("--version", default=None)
    parser.add_argument("--limit", type=_positive_int, default=10)


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return value


def _nonnegative_int(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return value


def _json_dump(value: Any) -> None:
    """Write one JSON value to stdout and keep logs off the protocol stream."""

    sys.stdout.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def _load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("config must contain a JSON object")
    return value


def _open_store(path: Path):
    # Import lazily so ``--help`` remains useful when optional runtime imports
    # are unavailable and so CLI argument failures do not touch the database.
    from .store import Store

    return Store(path)


def _open_readonly_store(path: Path):
    """Open an existing index without creating or migrating it."""

    from .store import Store

    return Store(path, readonly=True)


def _close_store(store: Any) -> None:
    close = getattr(store, "close", None)
    if callable(close):
        close()


def _run(args: argparse.Namespace) -> Any:
    db_path = (args.db or default_db_path()).expanduser()

    if args.command == "serve":
        from .server import run_stdio

        return run_stdio(db_path)

    if args.command == "sync":
        from .sources import sync_sources

        config = _load_config(args.config)
        store = _open_store(db_path)
        try:
            return sync_sources(store, config, source=args.source)
        finally:
            _close_store(store)

    store = _open_readonly_store(db_path)
    try:
        if args.command == "search":
            return store.search(
                args.query,
                source=args.source,
                repository=args.repository,
                version=args.version,
                limit=args.limit,
            )
        if args.command == "get":
            value = store.get_document(
                args.document_id,
                offset=args.offset,
                limit=args.limit,
            )
            if value is None:
                raise LookupError(f"document not found: {args.document_id}")
            return value
        if args.command == "section":
            value = store.get_section(args.document_id, args.section)
            if value is None:
                raise LookupError(
                    f"section not found: {args.document_id} / {args.section}"
                )
            return value
        if args.command == "sources":
            return store.list_sources()
        raise ValueError(f"unknown command: {args.command}")
    finally:
        _close_store(store)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a shell-compatible status code."""

    parser = _parser()
    try:
        args = parser.parse_args(argv)
        result = _run(args)
        # ``serve`` owns stdout because it is an MCP JSON-RPC stream.
        if args.command != "serve" and result is not None:
            _json_dump(result)
        if args.command == "sync" and isinstance(result, dict):
            # A failed fetch is a failed command even when some documents were
            # indexed successfully.  A bounded/truncated sync is a distinct
            # non-error status so automation can decide whether to retry.
            failed = result.get("failed", 0)
            try:
                failed_count = int(failed or 0)
            except (TypeError, ValueError):
                failed_count = 1 if failed else 0
            if failed_count > 0:
                print(
                    f"error: sync completed with {failed_count} failed item(s)",
                    file=sys.stderr,
                )
                return SYNC_FAILURE_EXIT
            partial = bool(result.get("partial") or result.get("truncated"))
            if partial:
                print(
                    "warning: sync completed with bounded or truncated coverage",
                    file=sys.stderr,
                )
                return SYNC_PARTIAL_EXIT
        return 0
    except SystemExit:
        # argparse uses SystemExit for --help and syntax failures.  Keep the
        # normal command-line behavior when called as a function in tests.
        raise
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
