"""Rebuild, verify and atomically publish the configured MCP index.

Run with .venv/bin/python scripts/refresh_index.py from any directory.
Failed or truncated crawls never replace the serving database.

GitHub's unauthenticated API allows few requests per hour, and the configured
organization now needs more than one window to finish.  The crawl therefore
runs GitHub in small batches into a single staging database and, when a batch
fails only because of API rate limiting, waits for the published reset time and
retries it instead of discarding the whole run.  Nothing is published until
every source and the MCP probes pass.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".data"
LIVE = DATA / "index.sqlite3"
GITHUB_BATCH_ENV = "METAX_REFRESH_GITHUB_BATCH"
DEFAULT_GITHUB_BATCH = 4
MAX_RATE_LIMIT_WAITS = 3
MAX_RATE_LIMIT_SLEEP = 3_700.0
RATE_LIMIT_MARGIN = 30.0
GITHUB_RATE_LIMIT_URL = "https://api.github.com/rate_limit"
GITHUB_API_HOST = "api.github.com"


def save(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_logged(command: list[str], output: Path, errors: Path) -> int:
    """Run *command*, capture stdout/stderr and return the exit status.

    The caller decides whether a non-zero status is fatal so that a
    rate-limited crawl can be inspected and retried instead of discarded.
    """

    with output.open("w") as out, errors.open("w") as err:
        result = subprocess.run(command, cwd=ROOT, stdout=out, stderr=err, check=False)
    return int(result.returncode or 0)


def read_report(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def github_batch_size() -> int:
    raw = os.environ.get(GITHUB_BATCH_ENV, "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_GITHUB_BATCH
    return value if 1 <= value <= 32 else DEFAULT_GITHUB_BATCH


def rate_limited(report: dict) -> bool:
    """True when every recorded failure is a GitHub API rate limit.

    A mixed report is not a rate-limit report: partial progress with a real
    error must stop the publication instead of waiting an hour for a retry.
    """

    errors = report.get("errors") or []
    if not errors:
        return False
    for error in errors:
        if not isinstance(error, dict):
            return False
        message = str(error.get("error", ""))
        url = str(error.get("url", ""))
        if GITHUB_API_HOST not in url or not any(token in message for token in ("403", "429")):
            return False
    return True


def wait_for_github_reset(label: str) -> None:
    """Sleep until GitHub's published rate-limit reset, then return."""

    headers = {"Accept": "application/vnd.github+json", "User-Agent": "metax-docs-mcp-refresh"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    delay = RATE_LIMIT_MARGIN
    try:
        with httpx.Client(timeout=30, headers=headers) as client:
            response = client.get(GITHUB_RATE_LIMIT_URL)
            response.raise_for_status()
            core = response.json()["resources"]["core"]
        remaining = int(core.get("remaining", 0))
        if remaining <= 0:
            delay = max(RATE_LIMIT_MARGIN, float(core["reset"]) - time.time() + RATE_LIMIT_MARGIN)
    except Exception as exc:  # A failed probe must not abort the crawl.
        print(f"Rate limit probe failed ({exc}); waiting {delay:.0f}s before retry", flush=True)
    delay = min(max(delay, 5.0), MAX_RATE_LIMIT_SLEEP)
    print(f"GitHub API rate limit reached during {label}; sleeping {delay:.0f}s", flush=True)
    time.sleep(delay)


def sync_with_retry(staging: Path, config_path: Path, directory: Path, label: str) -> dict:
    """Synchronize one config file, retrying only GitHub rate-limit failures."""

    waits = 0
    while True:
        output = directory / f"sync-{label}.json"
        errors = directory / f"sync-{label}.stderr.log"
        command = [str(ROOT / ".venv/bin/metax-docs-mcp"), "--db", str(staging), "sync", "--config", str(config_path)]
        exit_code = int(run_logged(command, output, errors) or 0)
        report = read_report(output)
        if exit_code == 0 and not report.get("failed"):
            return report
        if not rate_limited(report):
            raise RuntimeError(f"Command exited {exit_code}; see {output} and {errors}")
        waits += 1
        if waits > MAX_RATE_LIMIT_WAITS:
            raise RuntimeError(f"GitHub API rate limit persisted after {waits - 1} waits; see {output}")
        wait_for_github_reset(label)


def merge_reports(reports: list[dict]) -> dict:
    """Combine per-batch reports into the single report the run publishes."""

    counters = ("discovered", "indexed", "unchanged", "failed")
    merged: dict[str, Any] = {
        "source": "all",
        **{name: 0 for name in counters},
        "errors": [],
        "truncated": False,
        "partial": False,
        "coverage": {},
        "by_source": {},
    }
    for report in reports:
        for name in counters:
            merged[name] += int(report.get(name) or 0)
        merged["errors"].extend(report.get("errors") or [])
        merged["truncated"] = merged["truncated"] or bool(report.get("truncated"))
        merged["partial"] = merged["partial"] or bool(report.get("partial"))
        for name, stats in (report.get("by_source") or {}).items():
            entry = merged["by_source"].setdefault(
                name,
                {
                    "source": name,
                    **{field: 0 for field in counters},
                    "errors": [],
                    "truncated": False,
                    "partial": False,
                    "truncation_reasons": [],
                },
            )
            for field in counters:
                entry[field] += int(stats.get(field) or 0)
            entry["errors"].extend(stats.get("errors") or [])
            entry["truncated"] = entry["truncated"] or bool(stats.get("truncated"))
            entry["partial"] = entry["partial"] or bool(stats.get("partial"))
            for reason in stats.get("truncation_reasons") or []:
                if reason not in entry["truncation_reasons"]:
                    entry["truncation_reasons"].append(reason)
    for name, entry in merged["by_source"].items():
        entry["coverage"] = {
            "source": name,
            "complete": not bool(entry["partial"]),
            "partial": bool(entry["partial"]),
            "discovered": entry["discovered"],
            "indexed": entry["indexed"],
            "unchanged": entry["unchanged"],
            "failed": entry["failed"],
            "truncated": bool(entry["truncated"]),
            "truncation_reasons": list(entry["truncation_reasons"]),
        }
        merged["coverage"][name] = entry["coverage"]
    return merged


def sync_all(staging: Path, snapshot: Path, directory: Path) -> dict:
    """Synchronize every configured source into *staging*.

    Non-GitHub sources run once; GitHub runs in batches so a rate-limit window
    only has to cover a few repositories instead of the whole organization.
    Upserts are idempotent, so all batches share one staging database and the
    result is identical to a single crawl.
    """

    config = json.loads(snapshot.read_text())
    reports: list[dict] = []
    others = {name: value for name, value in config.items() if name != "github"}
    if any(name in config for name in ("official", "docker")):
        path = directory / "sources-other.json"
        save(path, others)
        reports.append(sync_with_retry(staging, path, directory, "other"))
    github = config.get("github")
    repositories = list(github.get("repositories") or []) if isinstance(github, dict) else []
    if repositories:
        size = github_batch_size()
        groups = [repositories[index : index + size] for index in range(0, len(repositories), size)]
        for index, group in enumerate(groups, start=1):
            path = directory / f"sources-github-{index:02d}.json"
            save(path, {"github": {**github, "repositories": group}, "http": config.get("http", {})})
            print(f"GitHub batch {index}/{len(groups)}: {', '.join(group)}", flush=True)
            reports.append(sync_with_retry(staging, path, directory, f"github-{index:02d}"))
    if not reports:
        raise RuntimeError("config selects no source to synchronize")
    return merge_reports(reports)


def counts(db: Path) -> dict[str, int]:
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as conn:
        return dict(conn.execute("SELECT source, count(*) FROM documents GROUP BY source"))


def current_config(path: Path) -> dict:
    config = json.loads(path.read_text())
    github = config.get("github", {})
    refs = github.get("repository_refs", {})
    repository = "MetaX-MACA/sglang"
    pattern = r"release/v(\d+)\.(\d+)\.(\d+)"
    # Keep explicit tag/commit pins. For the configured adaptation release,
    # discover the latest stable branch rather than freezing weekly refreshes.
    if not re.fullmatch(pattern, refs.get(repository, "")):
        return config
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "metax-docs-mcp-refresh"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    candidates = []
    try:
        with httpx.Client(timeout=60, headers=headers, follow_redirects=False) as client:
            for page in range(1, 11):
                response = client.get(f"https://api.github.com/repos/{repository}/branches", params={"per_page": 100, "page": page})
                response.raise_for_status()
                branches = response.json()
                if not isinstance(branches, list):
                    raise RuntimeError("Invalid SGLang branch response")
                for branch in branches:
                    match = re.fullmatch(pattern, branch.get("name", ""))
                    if match:
                        candidates.append((tuple(map(int, match.groups())), branch["name"]))
                if len(branches) < 100:
                    break
            else:
                raise RuntimeError("SGLang branch discovery exceeded pagination limit")
    except Exception as exc:
        # The pin can still be crawled when discovery is unavailable (for
        # example an exhausted API rate limit); keep the configured ref and
        # let the reported version show which release was actually indexed.
        print(f"SGLang release discovery skipped ({exc}); keeping {refs[repository]}", flush=True)
        return config
    if not candidates:
        raise RuntimeError("No stable SGLang adaptation release branches found")
    refs[repository] = max(candidates)[1]
    return config


def changes(staging: Path) -> dict[str, int]:
    with sqlite3.connect(f"{staging.as_uri()}?mode=ro", uri=True) as conn:
        conn.execute("ATTACH DATABASE ? AS previous", (str(LIVE),))
        return {
            "added": conn.execute("SELECT count(*) FROM main.documents n LEFT JOIN previous.documents o ON n.id=o.id WHERE o.id IS NULL").fetchone()[0],
            "removed": conn.execute("SELECT count(*) FROM previous.documents o LEFT JOIN main.documents n ON n.id=o.id WHERE n.id IS NULL").fetchone()[0],
            "updated": conn.execute("SELECT count(*) FROM main.documents n JOIN previous.documents o ON n.id=o.id WHERE n.content_hash<>o.content_hash OR n.url<>o.url OR n.revision<>o.revision OR n.version<>o.version OR n.metadata_json<>o.metadata_json").fetchone()[0],
        }


def refresh() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = DATA / "refresh" / stamp
    directory.mkdir(parents=True)
    staging = directory / "index.sqlite3"
    config = ROOT / "examples" / "sources.json"
    # Preserve the exact source selection used by this run.
    snapshot = directory / "sources.json"
    report = {"started_at": stamp, "published": False, "run_directory": str(directory)}
    save(directory / "result.json", report)
    try:
        save(snapshot, current_config(config))
        print(f"Rebuilding index; logs: {directory}", flush=True)
        sync = sync_all(staging, snapshot, directory)
        save(directory / "sync.json", sync)
        if sync.get("failed") or sync.get("partial") or sync.get("truncated"):
            raise RuntimeError("Crawl is incomplete; serving index was preserved")
        expected = {name for name in ("official", "github", "docker") if name in json.loads(snapshot.read_text())}
        collected = counts(staging)
        if set(collected) != expected or any(value <= 0 for value in collected.values()):
            raise RuntimeError(f"Missing source coverage: {collected}")
        with sqlite3.connect(staging) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite integrity check failed")
            conn.execute("INSERT INTO documents_fts(documents_fts) VALUES('integrity-check')")
        print("Crawl complete; verifying all sources through stdio MCP", flush=True)
        verify_exit = run_logged(
            [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/verify_expanded.py"), str(staging)],
            directory / "verify.json", directory / "verify.stderr.log",
        )
        if verify_exit:
            raise RuntimeError(f"MCP verification exited {verify_exit}; see {directory / 'verify.stderr.log'}")
        report["counts"] = collected
        if LIVE.exists():
            report["before_counts"] = counts(LIVE)
            report["changes"] = changes(staging)
            backup = DATA / "index.previous.sqlite3"
            temporary_backup = directory / "previous.sqlite3"
            with sqlite3.connect(f"{LIVE.as_uri()}?mode=ro", uri=True) as old:
                with sqlite3.connect(temporary_backup) as destination:
                    old.backup(destination)
            os.replace(temporary_backup, backup)
            report["backup"] = str(backup)
        # Store uses rollback journals and MCP opens a fresh connection per
        # request, so same-filesystem replacement exposes a complete snapshot.
        if any(Path(str(LIVE) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise RuntimeError("Serving database has active SQLite sidecars; publication stopped")
        with staging.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(staging, LIVE)
        report["published"] = True
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save(directory / "result.json", report)
        save(DATA / "refresh-latest.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        return 0
    except Exception as exc:
        report["error"] = str(exc)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save(directory / "result.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    DATA.mkdir(exist_ok=True)
    with (DATA / "refresh.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another index refresh is already running")
        raise SystemExit(refresh())
