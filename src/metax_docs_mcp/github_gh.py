"""Read GitHub through the logged-in gh CLI without exporting credentials."""
from __future__ import annotations

import os
import base64
import json
import hashlib
import re
import selectors
import subprocess
import tempfile
import time
from urllib.parse import urlsplit


def api(endpoint: str, *, max_bytes: int, timeout: float = 120) -> tuple[bytes, bool]:
    """Return a bounded gh API response and whether its body was truncated."""
    return _read(["gh", "api", "--method", "GET", endpoint], max_bytes=max_bytes, timeout=timeout)


def blob(owner: str, repository: str, sha: str, *, max_bytes: int, timeout: float = 120) -> dict:
    """Read a fixed blob through gh GraphQL after a REST network failure."""
    query = 'query { repository(owner:%s,name:%s) { object(oid:%s) { ... on Blob { oid byteSize isBinary isTruncated text } } } }' % tuple(json.dumps(value) for value in (owner, repository, sha))
    for attempt in range(3):
        try:
            raw, truncated = _read(["gh", "api", "graphql", "-f", "query=" + query], max_bytes=max_bytes, timeout=timeout)
            break
        except RuntimeError as exc:
            if attempt == 2 or str(exc) not in {"gh api failed (exit 1)", "gh api request timed out"}:
                raise
            time.sleep(.25 * (2 ** attempt))
    payload = json.loads(raw)
    if truncated or payload.get("errors"):
        raise RuntimeError("gh GraphQL blob response is incomplete")
    value = (payload.get("data", {}).get("repository") or {}).get("object")
    if not value or value.get("oid") != sha or value.get("isBinary") or value.get("isTruncated") or not isinstance(value.get("text"), str):
        raise RuntimeError("gh GraphQL did not return a complete text blob")
    data = value["text"].encode("utf-8")
    if len(data) != value.get("byteSize"):
        raise RuntimeError("gh GraphQL blob byte size mismatch")
    if hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() != sha:
        raise RuntimeError("gh GraphQL text does not match the requested Git blob")
    return {"encoding": "base64", "content": base64.b64encode(data).decode("ascii")}


def _read(command: list[str], *, max_bytes: int, timeout: float) -> tuple[bytes, bool]:
    with tempfile.TemporaryFile() as errors:
        try:
            child = subprocess.Popen(
                command,
                stdout=subprocess.PIPE, stderr=errors,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("gh is required for github.client=gh") from exc
        chunks: list[bytes] = []
        size = 0
        deadline = time.monotonic() + timeout
        try:
            assert child.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise RuntimeError("gh api request timed out")
                    chunk = os.read(child.stdout.fileno(), min(65_536, max_bytes + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > max_bytes:
                        return b"".join(chunks)[:max_bytes], True
            code = child.wait(timeout=max(0.01, deadline - time.monotonic()))
            if code:
                errors.seek(0)
                message = errors.read(4096).decode("utf-8", "replace")
                status = re.search(r"HTTP (\d{3})", message)
                # gh owns authentication. Never copy arbitrary stderr, which
                # could contain a response body or credential-bearing URL.
                raise RuntimeError(f"gh api failed (HTTP {status[1]})" if status else f"gh api failed (exit {code})")
            return b"".join(chunks), False
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()
            if child.stdout is not None:
                child.stdout.close()


def endpoint_for_url(url: str) -> str:
    """Accept the public API and fixed-commit codeload URLs used by ingestion."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("GitHub URL must use public HTTPS")
    if parsed.hostname == "api.github.com":
        return parsed.path.lstrip("/") + ("?" + parsed.query if parsed.query else "")
    if parsed.hostname == "codeload.github.com":
        match = re.fullmatch(r"/([\w.-]+)/([\w.-]+)/tar\.gz/([0-9a-fA-F]{40})", parsed.path)
        if match and not parsed.query:
            return f"repos/{match[1]}/{match[2]}/tarball/{match[3]}"
    raise ValueError("GitHub URL is outside the gh ingestion allowlist")
