"""Bounded ingestion for the public MetaX documentation sources.

The index is deliberately kept local.  This module only performs work when the
``sync_sources`` function is called; the MCP server must not import it as part
of serving read-only queries.

The implementation has three useful properties for an offline index:

* every URL is checked against an HTTPS host allowlist before the request and
  after every redirect;
* all page, repository, response, and retry counts have hard upper bounds;
* a failed request never makes an existing document eligible for deletion.

``httpx``, BeautifulSoup and markdownify are declared package dependencies.  A
small stdlib HTML fallback is retained so that configuration validation and
unit tests can still run when optional parsing packages are unavailable.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html as html_lib
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

try:  # The dependency is installed by the package, but keep import failures clear.
    import httpx
except ImportError:  # pragma: no cover - exercised only in an incomplete environment
    httpx = None  # type: ignore[assignment]

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - fallback is tested without bs4
    BeautifulSoup = None  # type: ignore[assignment,misc]

try:
    from markdownify import markdownify
except ImportError:  # pragma: no cover - fallback is tested without markdownify
    markdownify = None  # type: ignore[assignment]

try:
    from .models import Document
except (ImportError, ModuleNotFoundError):  # Allows this module to be imported first.
    Document = None  # type: ignore[assignment,misc]


__all__ = ["sync_sources"]


# Bounds are intentionally independent of user supplied configuration.  They
# prevent a malformed config from turning a one-shot sync into an unbounded
# crawler or a memory exhaustion path.
HARD_MAX_PAGES = 1_000
HARD_MAX_FILES_PER_REPO = 5_000
HARD_MAX_REPOSITORIES = 32
HARD_MAX_SEEDS = 32
HARD_MAX_REDIRECTS = 3
HARD_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
HARD_MAX_DOCUMENT_CHARS = 500_000
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_DOCUMENT_CHARS = 250_000
DEFAULT_USER_AGENT = "metax-docs-mcp/0.1 (bounded public-source indexer)"
GITHUB_API_ROOT = "https://api.github.com"
GITHUB_WEB_ROOT = "https://github.com"
OFFICIAL_HOST = "developer.metax-tech.com"
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
_NON_HTML_SUFFIXES = {
    ".7z",
    ".avi",
    ".bmp",
    ".css",
    ".csv",
    ".doc",
    ".docx",
    ".eot",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".svg",
    ".tar",
    ".tgz",
    ".ttf",
    ".wav",
    ".webp",
    ".woff",
    ".woff2",
    ".xls",
    ".xlsx",
    ".zip",
}
_DOC_EXTENSIONS = {".md", ".mdx", ".markdown", ".rst"}
_CODE_EXTENSIONS = {
    ".asm",
    ".c",
    ".cc",
    ".cfg",
    ".cmake",
    ".cpp",
    ".csh",
    ".cu",
    ".cuh",
    ".fish",
    ".go",
    ".h",
    ".hh",
    ".hpp",
    ".in",
    ".ini",
    ".java",
    ".jl",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".lua",
    ".m",
    ".mm",
    ".py",
    ".pyi",
    ".r",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".tcl",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
    ".zsh",
}
_SPECIAL_CODE_NAMES = {
    "BUILD",
    "BUCK",
    "CMakeLists.txt",
    "Dockerfile",
    "GNUmakefile",
    "Makefile",
    "WORKSPACE",
}
_OFFICIAL_SKIP_BASENAMES = {"search.html", "genindex.html", "py-modindex.html"}


class _IngestionError(RuntimeError):
    """An expected source failure whose message is safe to return to callers."""


@dataclass(frozen=True)
class _FetchResult:
    url: str
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    truncated: bool = False


@dataclass(frozen=True)
class _ParsedPage:
    title: str
    text: str
    links: tuple[str, ...]
    content_type: str = ""
    rejected_reason: str = ""
    truncated: bool = False


@dataclass(frozen=True)
class _OfficialCatalogEntry:
    url: str
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class _GithubFile:
    path: str
    sha: str
    size: int
    kind: str = "blob"


class _FallbackHTMLParser(HTMLParser):
    """Small HTML parser used only if BeautifulSoup is unavailable.

    It intentionally extracts links, headings, code and visible text rather
    than trying to implement a complete HTML-to-Markdown converter.
    """

    _ignored = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self._stack: list[str] = []
        self._in_title = False
        self._in_heading: str | None = None
        self._in_pre = False
        self._pre_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_map = dict(attrs)
        if tag == "a" and attrs_map.get("href"):
            self.links.append(str(attrs_map["href"]))
        self._stack.append(tag)
        if tag == "title":
            self._in_title = True
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._in_heading = tag
            self.text_parts.append("\n" + "#" * int(tag[1]) + " ")
        if tag == "pre":
            self._in_pre = True
            self._pre_parts = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._in_heading = None
            self.text_parts.append("\n")
        if tag == "pre":
            self._in_pre = False
            self.text_parts.extend(["\n```\n", "".join(self._pre_parts), "\n```\n"])
            self._pre_parts = []
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index] == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if not data or any(tag in self._ignored for tag in self._stack):
            return
        if self._in_title:
            self.title_parts.append(data)
        if self._in_pre:
            self._pre_parts.append(data)
        else:
            self.text_parts.append(data)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _stable_id(source: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()
    return f"{source}:{digest}"


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed:  # NaN
        return default
    return min(max(parsed, minimum), maximum)


def _canonical_url(value: str, base: str | None = None) -> str:
    if not isinstance(value, str):
        raise _IngestionError("URL must be a string")
    raw = urljoin(base or "", value.strip())
    try:
        parts = urlsplit(raw)
        host = parts.hostname
        if not host or parts.username or parts.password:
            raise ValueError("missing host or URL credentials")
        # Normalise the host but preserve URL path spelling, since encoded
        # Chinese paths can be meaningful to the origin server.
        host = host.lower()
        port = parts.port
        netloc = host
        if port and not ((parts.scheme.lower() == "https" and port == 443) or (parts.scheme.lower() == "http" and port == 80)):
            netloc = f"{host}:{port}"
        path = parts.path or "/"
        # Fragments are client-side navigation and cannot identify a source
        # document.  Keep query parameters, sorted only for stable IDs.
        query_pairs = parse_qsl(parts.query, keep_blank_values=True)
        query = urlencode(sorted(query_pairs), doseq=True)
        return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))
    except ValueError as exc:
        raise _IngestionError("invalid URL") from exc


def _allowed_url(url: str, allowed_hosts: set[str]) -> bool:
    try:
        parts = urlsplit(url)
        return (
            parts.scheme == "https"
            and bool(parts.hostname)
            and parts.port in (None, 443)
            and not parts.username
            and not parts.password
            and (parts.hostname or "").lower() in allowed_hosts
        )
    except ValueError:
        return False


def _safe_error(error: BaseException, secret: str | None = None) -> str:
    message = str(error).strip() or error.__class__.__name__
    if secret:
        message = message.replace(secret, "[redacted]")
    # Do not return arbitrary response bodies or multi-megabyte exception text.
    message = re.sub(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[^\s,;]+", r"\1[redacted]", message)
    return message[:500]


class _RateLimiter:
    def __init__(self, interval: float, sleep: Callable[[float], None] = time.sleep) -> None:
        self.interval = interval
        self.sleep = sleep
        self._next = 0.0

    def wait(self) -> None:
        if self.interval <= 0:
            return
        now = time.monotonic()
        if now < self._next:
            self.sleep(self._next - now)
        self._next = time.monotonic() + self.interval


class _HttpFetcher:
    """HTTP GET wrapper with bounded body reads and checked redirects."""

    def __init__(
        self,
        client: Any,
        *,
        allowed_hosts: set[str],
        retries: int,
        max_response_bytes: int,
        rate_limiter: _RateLimiter,
        secret: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.allowed_hosts = allowed_hosts
        self.retries = retries
        self.max_response_bytes = max_response_bytes
        self.rate_limiter = rate_limiter
        self.secret = secret
        self.sleep = sleep

    def get(self, url: str) -> _FetchResult:
        current = _canonical_url(url)
        if not _allowed_url(current, self.allowed_hosts):
            raise _IngestionError("URL is outside the public HTTPS allowlist")
        redirects = 0
        while True:
            result = self._get_once(current)
            if result.status_code not in _REDIRECT_STATUSES:
                if result.status_code < 200 or result.status_code >= 300:
                    raise _IngestionError(f"HTTP {result.status_code}")
                return result
            if redirects >= HARD_MAX_REDIRECTS:
                raise _IngestionError("redirect limit exceeded")
            location = result.headers.get("location", "")
            if not location:
                raise _IngestionError("redirect has no Location header")
            try:
                next_url = _canonical_url(location, current)
            except _IngestionError as exc:
                raise _IngestionError("redirect target is invalid") from exc
            if not _allowed_url(next_url, self.allowed_hosts):
                raise _IngestionError("redirect target is outside the public HTTPS allowlist")
            current = next_url
            redirects += 1

    def _get_once(self, url: str) -> _FetchResult:
        last_error: BaseException | None = None
        for attempt in range(self.retries + 1):
            self.rate_limiter.wait()
            try:
                # ``stream`` lets us stop reading at the configured bound.  A
                # few lightweight HTTP test doubles expose only ``get``; that
                # fallback still truncates the retained body before returning.
                if hasattr(self.client, "stream"):
                    with self.client.stream("GET", url) as response:
                        result = self._read_response(response, url)
                else:  # pragma: no cover - only custom external test doubles
                    result = self._read_response(self.client.get(url), url)
                if result.status_code in _RETRY_STATUSES and attempt < self.retries:
                    self._backoff(attempt, result.headers)
                    continue
                return result
            except Exception as exc:  # httpx.RequestError is not always available in fakes
                last_error = exc
                if attempt >= self.retries:
                    break
                self._backoff(attempt, {})
        assert last_error is not None
        raise _IngestionError(_safe_error(last_error, self.secret)) from last_error

    def _read_response(self, response: Any, url: str) -> _FetchResult:
        headers = {str(k).lower(): str(v) for k, v in getattr(response, "headers", {}).items()}
        status = int(getattr(response, "status_code", 0))
        if status in _REDIRECT_STATUSES:
            return _FetchResult(url, status, headers, b"", False)
        content_length = headers.get("content-length")
        too_large = False
        if content_length:
            try:
                too_large = int(content_length) > self.max_response_bytes
            except ValueError:
                pass
        chunks: list[bytes] = []
        total = 0
        iterator = getattr(response, "iter_bytes", None)
        if callable(iterator):
            for chunk in iterator():
                if not chunk:
                    continue
                remaining = self.max_response_bytes + 1 - total
                if remaining <= 0:
                    too_large = True
                    break
                piece = bytes(chunk[:remaining])
                chunks.append(piece)
                total += len(piece)
                if total > self.max_response_bytes:
                    too_large = True
                    break
        else:
            body = bytes(getattr(response, "content", b""))
            too_large = too_large or len(body) > self.max_response_bytes
            chunks.append(body[: self.max_response_bytes + 1])
        content = b"".join(chunks)
        if len(content) > self.max_response_bytes:
            content = content[: self.max_response_bytes]
            too_large = True
        return _FetchResult(url, status, headers, content, too_large)

    def _backoff(self, attempt: int, headers: Mapping[str, str]) -> None:
        retry_after = headers.get("retry-after", "")
        delay: float
        try:
            delay = min(float(retry_after), 10.0)
        except (TypeError, ValueError):
            delay = min(0.25 * (2**attempt), 5.0)
        if delay > 0:
            self.sleep(delay)


def _new_client(timeout: float, headers: Mapping[str, str]) -> Any:
    if httpx is None:
        raise RuntimeError("httpx is required for source synchronization")
    return httpx.Client(timeout=timeout, headers=dict(headers), follow_redirects=False)


def _new_stats(source: str) -> dict[str, Any]:
    return {
        "source": source,
        "discovered": 0,
        "indexed": 0,
        "unchanged": 0,
        "failed": 0,
        "errors": [],
        "truncated": False,
        "partial": False,
        "truncation_reasons": [],
        "coverage": {
            "source": source,
            "complete": True,
            "partial": False,
            "discovered": 0,
            "indexed": 0,
            "unchanged": 0,
            "failed": 0,
            "truncated": False,
            "truncation_reasons": [],
        },
    }


def _add_error(stats: dict[str, Any], *, url: str, error: BaseException | str, path: str = "") -> None:
    message = _safe_error(error) if isinstance(error, BaseException) else str(error)[:500]
    item: dict[str, str] = {"url": url, "error": message}
    if path:
        item["path"] = path
    stats["errors"].append(item)
    stats["failed"] += 1
    stats["partial"] = True


def _mark_truncated(stats: dict[str, Any], reason: str) -> None:
    stats["truncated"] = True
    stats["partial"] = True
    reasons = stats["truncation_reasons"]
    if reason not in reasons:
        reasons.append(reason)


def _finish_stats(stats: dict[str, Any]) -> None:
    stats["coverage"] = {
        "source": stats["source"],
        "complete": not bool(stats["partial"]),
        "partial": bool(stats["partial"]),
        "discovered": stats["discovered"],
        "indexed": stats["indexed"],
        "unchanged": stats["unchanged"],
        "failed": stats["failed"],
        "truncated": bool(stats["truncated"]),
        "truncation_reasons": list(stats["truncation_reasons"]),
    }


def _parse_html(content: bytes, *, max_document_chars: int) -> _ParsedPage:
    decoded = content.decode("utf-8", "replace")
    if BeautifulSoup is None:
        parser = _FallbackHTMLParser()
        parser.feed(decoded)
        title = " ".join(parser.title_parts).strip()
        text = _normalise_text("".join(parser.text_parts), max_document_chars)
        return _ParsedPage(title, text, tuple(parser.links), "")

    soup = BeautifulSoup(decoded, "html.parser")
    title_node = soup.find("title")
    title = title_node.get_text(" ", strip=True) if title_node else ""
    # Next.js and other client-side shells can return HTTP 200 with almost no
    # usable body.  This is rejected before it can pollute the local index.
    for tag in soup.find_all(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    root = None
    for selector in (
        "[itemprop='articleBody']",
        "main",
        "article",
        "div.document",
        "[role='main']",
        ".rst-content",
        ".markdown-body",
    ):
        root = soup.select_one(selector)
        if root is not None:
            break
    if root is None:
        root = soup.body or soup
        # Remove site chrome only when body is the chosen root.  A Sphinx
        # document's ``main``/``div.document`` may contain legitimate headers.
        for tag in root.find_all(["nav", "footer", "aside", "header"]):
            tag.decompose()
    for permalink in soup.select("a.headerlink"):
        permalink.decompose()
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        links.append(str(anchor.get("href")))
    for link in soup.find_all("link", href=True):
        rel = {str(x).lower() for x in (link.get("rel") or [])}
        if rel.intersection({"next", "contents", "index"}):
            links.append(str(link.get("href")))
    # The Next.js directory occasionally embeds preview URLs in an RSC payload;
    # capture those without fetching arbitrary script URLs.
    links.extend(_embedded_preview_links(decoded))

    if markdownify is not None:
        converted = markdownify(str(root), heading_style="ATX", bullets="-")
    else:  # pragma: no cover - fallback path tested only without markdownify
        converted = root.get_text("\n", strip=False)
    text = _normalise_text(converted, max_document_chars)

    lower_title = title.lower()
    lower_text = text.lower()
    error_markers = (
        "404",
        "not found",
        "page not found",
        "internal server error",
        "access denied",
        "error page",
        "不存在",
        "页面不存在",
        "访问被拒绝",
    )
    if any(marker in lower_title for marker in error_markers) or (
        len(text) < 80 and any(marker in lower_text for marker in error_markers)
    ):
        return _ParsedPage(title, "", tuple(links), "", "HTTP 200 error page")
    if len(text.strip()) < 80 and not root.find(["h1", "h2", "h3"]):
        return _ParsedPage(title, "", tuple(links), "", "HTTP 200 client-side shell or empty document")
    return _ParsedPage(title, text, tuple(links), "", truncated=len(converted) > max_document_chars)


def _embedded_preview_links(value: str) -> list[str]:
    # URL text is escaped in React server payloads.  Only the fixed official
    # path prefix is accepted; arbitrary embedded URLs are intentionally not.
    unescaped = html_lib.unescape(value).replace("\\/", "/")
    pattern = re.compile(r"(?:https://developer\.metax-tech\.com)?/api/client/document/preview/[A-Za-z0-9_./%\-\u4e00-\u9fff]+(?:\.html)?")
    seen: set[str] = set()
    result: list[str] = []
    for match in pattern.finditer(unescaped):
        item = match.group(0)
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _normalise_text(value: str, limit: int) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{4,}", "\n\n\n", value)
    value = value.strip()
    return value[:limit]


def _page_links(page: _ParsedPage, base: str, allowed_hosts: set[str]) -> Iterator[str]:
    seen: set[str] = set()
    base_path = urlsplit(base).path
    manual_root = base_path.split("/split_files/", 1)[0] + "/" if "/split_files/" in base_path else base_path.rsplit("/", 1)[0] + "/"
    for href in page.links:
        if not href or href.startswith(("#", "mailto:", "javascript:", "data:")):
            continue
        try:
            candidate = _canonical_url(href, base)
        except _IngestionError:
            continue
        if candidate in seen or not _allowed_url(candidate, allowed_hosts):
            continue
        candidate_path = urlsplit(candidate).path
        if not candidate_path.startswith(manual_root):
            continue
        path = candidate_path.lower()
        if path.endswith(tuple(_NON_HTML_SUFFIXES)):
            continue
        if path.rsplit("/", 1)[-1] in _OFFICIAL_SKIP_BASENAMES:
            continue
        seen.add(candidate)
        yield candidate


def _document_class() -> Any:
    if Document is None:
        raise RuntimeError("metax_docs_mcp.models.Document is required for ingestion")
    return Document


def _build_document(**fields: Any) -> Any:
    return _document_class()(**fields)


def _store_document(store: Any, document: Any, stats: dict[str, Any]) -> None:
    try:
        changed = bool(store.upsert(document))
    except Exception as exc:
        _add_error(stats, url=str(getattr(document, "url", "")), error=exc)
        return
    if changed:
        stats["indexed"] += 1
    else:
        stats["unchanged"] += 1


def _official_page_document(
    page: _ParsedPage,
    result: _FetchResult,
    *,
    max_document_chars: int,
    catalog_metadata: Mapping[str, Any] | None = None,
) -> Any:
    url = _canonical_url(result.url)
    text = _normalise_text(page.text, max_document_chars)
    title = page.title.strip() or urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] or url
    metadata = {
        "canonical_url": url,
        "content_sha256": _content_hash(text),
        "content_type": result.headers.get("content-type", ""),
        "last_modified": result.headers.get("last-modified", ""),
        "kind": "html",
        "content_truncated": page.truncated or result.truncated,
    }
    if catalog_metadata:
        metadata.update({str(key): value for key, value in catalog_metadata.items() if value not in (None, "")})
    # The document exposed by the directory is the latest preview.  Keep both
    # catalog fields in metadata, while ``Document.version`` is the selected
    # latest value used by the store's version filter.
    version = str(metadata.get("version") or metadata.get("latest_version") or "")
    return _build_document(
        id=_stable_id("official", url),
        source="official",
        title=title[:1_000],
        url=url,
        text=text,
        version=version,
        repository="",
        path=urlsplit(url).path,
        revision="",
        fetched_at=_now(),
        metadata=metadata,
    )


def _official_catalog_entries(
    fetcher: _HttpFetcher,
    *,
    max_entries: int = HARD_MAX_PAGES,
) -> tuple[list[_OfficialCatalogEntry], list[str]]:
    """Read the bounded public document-series catalog.

    The directory page is a client-side shell.  Its server API is a compact
    JSON list of document series.  ``code=-1`` can be returned with HTTP 200,
    so this function validates the application-level response before accepting
    any entries.
    """

    max_entries = _bounded_int(max_entries, HARD_MAX_PAGES, 1, HARD_MAX_PAGES)
    page_size = 100
    raw_items: list[Mapping[str, Any]] = []
    reasons: list[str] = []
    total_count: int | None = None
    max_catalog_pages = max(1, (max_entries + page_size - 1) // page_size)
    for page_number in range(1, max_catalog_pages + 1):
        endpoint = (
            f"https://{OFFICIAL_HOST}/api/client/document/series/filter/"
            f"?page={page_number}&page_size={page_size}"
        )
        try:
            response = fetcher.get(endpoint)
            try:
                payload = json.loads(response.content.decode("utf-8", "replace"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise _IngestionError("official catalog returned invalid JSON") from exc
            if response.truncated:
                raise _IngestionError("official catalog exceeded the response bound")
            if not isinstance(payload, Mapping):
                raise _IngestionError("official catalog response is not an object")
            code = payload.get("code")
            if code not in (None, 0, "0", "success", "SUCCESS"):
                message = payload.get("message") or payload.get("msg") or f"code={code}"
                raise _IngestionError(f"official catalog error: {str(message)[:200]}")
            data: Any = payload.get("data", payload)
            page_info: Any = payload.get("page_info") or payload.get("pagination")
            if isinstance(data, Mapping):
                page_info = page_info or data.get("page_info") or data.get("pagination")
                for key in ("data", "items", "results", "list", "rows"):
                    if isinstance(data.get(key), list):
                        data = data[key]
                        break
            if not isinstance(data, list):
                raise _IngestionError("official catalog response has no data list")
            if total_count is None:
                candidates = [payload, page_info]
                if isinstance(data, Mapping):
                    candidates.append(data)
                for candidate in candidates:
                    if not isinstance(candidate, Mapping):
                        continue
                    for key in ("total_count", "total", "count"):
                        try:
                            if candidate.get(key) is not None:
                                total_count = max(0, int(candidate[key]))
                                break
                        except (TypeError, ValueError):
                            continue
                    if total_count is not None:
                        break
            raw_items.extend(item for item in data if isinstance(item, Mapping))
            if total_count is not None:
                if len(raw_items) >= min(total_count, max_entries):
                    break
            elif len(data) < page_size:
                break
        except Exception as exc:
            # Preserve the successfully fetched first pages; the caller will
            # report this as partial while indexing those entries.
            reasons.append(f"catalog page {page_number}: {_safe_error(exc)}")
            break
    if total_count is not None and total_count > max_entries:
        reasons.append(f"official catalog limit {max_entries} of {total_count}")
    elif len(raw_items) >= max_entries and (total_count is None or total_count > len(raw_items)):
        reasons.append(f"official catalog page limit {max_catalog_pages}")

    entries: list[_OfficialCatalogEntry] = []
    seen: set[str] = set()
    first_endpoint = (
        f"https://{OFFICIAL_HOST}/api/client/document/series/filter/"
        "?page=1&page_size=100"
    )
    for item in raw_items[:max_entries]:
        # Current responses expose file_url and preview_file_id.  Keep the
        # fallback IDs for older catalog versions and test fixtures.
        versions = item.get("versions")
        latest: Mapping[str, Any] = {}
        if isinstance(versions, list) and versions:
            candidate_versions = [version for version in versions if isinstance(version, Mapping)]
            wanted_version = item.get("latest_version") or item.get("version")
            if wanted_version:
                latest = next(
                    (version for version in candidate_versions if version.get("version") == wanted_version),
                    candidate_versions[0] if candidate_versions else {},
                )
            elif candidate_versions:
                latest = candidate_versions[0]
        raw_url = (
            item.get("file_url")
            or item.get("preview_url")
            or item.get("url")
            or latest.get("file_url")
            or latest.get("preview_url")
            or latest.get("url")
        )
        preview_id = (
            item.get("preview_file_id")
            or item.get("file_id")
            or latest.get("preview_file_id")
            or latest.get("file_id")
        )
        if raw_url:
            try:
                candidate = _canonical_url(str(raw_url), f"https://{OFFICIAL_HOST}")
            except _IngestionError:
                candidate = ""
        elif preview_id not in (None, ""):
            candidate = f"https://{OFFICIAL_HOST}/api/client/document/preview/{quote(str(preview_id), safe='')}/index.html"
        else:
            candidate = ""
        if not candidate or not _allowed_url(candidate, fetcher.allowed_hosts):
            continue
        # Catalog URLs should resolve to a preview page.  Reject API/download
        # links that could otherwise make the crawler fetch unrelated content.
        if "/api/client/document/preview/" not in urlsplit(candidate).path:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        item_version = item.get("version") or ""
        latest_version = item.get("latest_version") or ""
        if not latest_version and latest:
            latest_version = latest.get("version") or latest.get("name") or ""
        metadata = {
            "series_name": item.get("series_name") or item.get("name") or "",
            "file_id": item.get("file_id") or "",
            "preview_file_id": item.get("preview_file_id") or "",
            "version": item_version,
            "latest_version": latest_version,
            "catalog_url": first_endpoint,
            "category_id": item.get("category_id"),
            "series_id": item.get("series_id"),
            "chip_series": item.get("chip_series", []),
            "detail_params": item.get("detail_params", {}),
        }
        detail_params = item.get("detail_params") or latest.get("detail_params")
        if detail_params not in (None, ""):
            try:
                encoded_detail_params = json.dumps(detail_params, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                encoded_detail_params = str(detail_params)
            metadata["detail_params"] = encoded_detail_params[:8_192]
        entries.append(_OfficialCatalogEntry(candidate, metadata))
    return entries, reasons


def _sync_official(
    store: Any,
    config: Mapping[str, Any],
    fetcher: _HttpFetcher,
    stats: dict[str, Any],
    *,
    max_document_chars: int,
) -> None:
    raw_seeds = config.get("seeds") or []
    if not isinstance(raw_seeds, Sequence) or isinstance(raw_seeds, (str, bytes, bytearray)):
        _add_error(stats, url="", error="official.seeds must be a list")
        return
    if len(raw_seeds) > HARD_MAX_SEEDS:
        _mark_truncated(stats, f"seed limit {HARD_MAX_SEEDS}")
    seeds: list[str] = []
    for raw in list(raw_seeds)[:HARD_MAX_SEEDS]:
        try:
            seed = _canonical_url(str(raw))
        except _IngestionError as exc:
            _add_error(stats, url=str(raw), error=exc)
            continue
        if not _allowed_url(seed, fetcher.allowed_hosts):
            _add_error(stats, url=seed, error="seed is outside the public HTTPS allowlist")
            continue
        if seed not in seeds:
            seeds.append(seed)
    if not seeds:
        _add_error(stats, url="", error="no valid official seeds; seed a static preview page or catalog")
        return

    max_pages_requested = _bounded_int(config.get("max_pages", 100), 100, 0, HARD_MAX_PAGES)
    discover = bool(config.get("discover", True))
    # A directory seed is a dynamic Next.js shell.  Resolve it through the
    # bounded first-party catalog API; a static preview seed is crawled only
    # through its own HTML links, which keeps a smoke sync scoped to one manual.
    catalog_metadata: dict[str, Mapping[str, Any]] = {}
    queue: deque[str] = deque()
    queued: set[str] = set()
    directory_seeds = [seed for seed in seeds if urlsplit(seed).path.rstrip("/") == "/doc"]
    if directory_seeds and discover:
        try:
            catalog_entries, catalog_reasons = _official_catalog_entries(fetcher)
            for entry in catalog_entries:
                if entry.url not in queued:
                    queue.append(entry.url)
                    queued.add(entry.url)
                    catalog_metadata[entry.url] = entry.metadata
            for reason in catalog_reasons:
                if reason.startswith("catalog page ") and ": " in reason:
                    _add_error(stats, url=directory_seeds[0], error=reason)
                else:
                    _mark_truncated(stats, reason)
            if not catalog_entries:
                _add_error(stats, url=directory_seeds[0], error="official catalog returned no preview documents")
        except Exception as exc:
            _add_error(stats, url=directory_seeds[0], error=exc)
        # Do not fetch the shell itself as a document.
        seeds = [seed for seed in seeds if seed not in directory_seeds]
    for seed in seeds:
        if seed not in queued:
            queue.append(seed)
            queued.add(seed)
    visited: set[str] = set()
    while queue and len(visited) < max_pages_requested:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        stats["discovered"] += 1
        try:
            response = fetcher.get(url)
            if response.truncated:
                _mark_truncated(stats, f"response body limit {fetcher.max_response_bytes} bytes")
            content_type = response.headers.get("content-type", "").lower()
            if content_type and not any(kind in content_type for kind in ("html", "xhtml", "text/plain")):
                raise _IngestionError(f"unsupported content type {content_type[:80]}")
            page = _parse_html(response.content, max_document_chars=max_document_chars)
            if page.rejected_reason:
                raise _IngestionError(page.rejected_reason)
            if page.truncated:
                _mark_truncated(stats, f"document character limit {max_document_chars}: {url}")
            document = _official_page_document(
                page,
                response,
                max_document_chars=max_document_chars,
                catalog_metadata=catalog_metadata.get(response.url),
            )
            _store_document(store, document, stats)
            if discover:
                inherited_metadata = catalog_metadata.get(response.url, {})
                for child in _page_links(page, response.url, fetcher.allowed_hosts):
                    if child not in visited and child not in queued:
                        queue.append(child)
                        queued.add(child)
                        if inherited_metadata:
                            catalog_metadata[child] = inherited_metadata
        except Exception as exc:
            _add_error(stats, url=url, error=exc)
    if queue:
        _mark_truncated(stats, f"max_pages={max_pages_requested}")


def _github_path_allowed(path: str, include_code: bool) -> bool:
    if not path or path.startswith("/") or ".." in PurePosixPath(path).parts:
        return False
    name = path.rsplit("/", 1)[-1]
    lower = name.lower()
    suffix = PurePosixPath(name).suffix.lower()
    if lower.startswith("readme") or suffix in _DOC_EXTENSIONS:
        return True
    if not include_code:
        return False
    return name in _SPECIAL_CODE_NAMES or suffix in _CODE_EXTENSIONS


def _normalise_repository(value: Any, organization: str) -> str:
    name = str(value or "").strip()
    if "/" in name:
        owner, _, repo = name.partition("/")
        if owner.lower() != organization.lower():
            raise _IngestionError("repository is outside configured organization")
        name = repo
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name):
        raise _IngestionError("invalid GitHub repository name")
    return name


def _github_json(fetcher: _HttpFetcher, url: str) -> tuple[Any, _FetchResult]:
    response = fetcher.get(url)
    try:
        value = json.loads(response.content.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _IngestionError("GitHub returned invalid JSON") from exc
    if response.truncated:
        raise _IngestionError("GitHub JSON response exceeded the response bound")
    return value, response


def _discover_github_repositories(
    organization: str,
    fetcher: _HttpFetcher,
    stats: dict[str, Any],
) -> list[str]:
    url = f"{GITHUB_API_ROOT}/orgs/{quote(organization, safe='')}/repos?type=public&per_page={HARD_MAX_REPOSITORIES}&page=1&sort=updated"
    try:
        payload, response = _github_json(fetcher, url)
        if not isinstance(payload, list):
            raise _IngestionError("GitHub organization response is not a list")
        names = [str(item.get("name", "")) for item in payload if isinstance(item, Mapping) and item.get("name")]
        names = [name for name in names if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name)]
        link = response.headers.get("link", "")
        if len(payload) >= HARD_MAX_REPOSITORIES or 'rel="next"' in link:
            _mark_truncated(stats, f"organization repository limit {HARD_MAX_REPOSITORIES}")
        if not names:
            raise _IngestionError("GitHub organization returned no public repositories")
        return names[:HARD_MAX_REPOSITORIES]
    except Exception as exc:
        _add_error(stats, url=url, error=exc)
        return []


def _github_files(tree_payload: Any, *, include_code: bool, max_files: int) -> tuple[list[_GithubFile], bool]:
    if not isinstance(tree_payload, Mapping) or not isinstance(tree_payload.get("tree"), list):
        raise _IngestionError("GitHub tree response has no tree")
    candidates: list[_GithubFile] = []
    for item in tree_payload["tree"]:
        if not isinstance(item, Mapping) or item.get("type") != "blob":
            continue
        path = str(item.get("path", ""))
        if not _github_path_allowed(path, include_code):
            continue
        sha = str(item.get("sha", ""))
        if not sha:
            continue
        try:
            size = max(0, int(item.get("size", 0)))
        except (TypeError, ValueError):
            size = 0
        candidates.append(_GithubFile(path=path, sha=sha, size=size))
    candidates.sort(key=lambda item: item.path.casefold())
    truncated = bool(tree_payload.get("truncated")) or len(candidates) > max_files
    return candidates[:max_files], truncated


def _github_title(path: str, text: str) -> str:
    for line in text.splitlines()[:20]:
        match = re.match(r"^\s{0,3}#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()[:1_000]
    return path.rsplit("/", 1)[-1] or path


def _github_document(
    *,
    organization: str,
    repository: str,
    branch: str,
    revision: str,
    item: _GithubFile,
    text: str,
    content_type: str,
    max_document_chars: int,
) -> Any:
    text = _normalise_text(text, max_document_chars)
    encoded_path = quote(item.path, safe="/")
    url = f"{GITHUB_WEB_ROOT}/{quote(organization, safe='')}/{quote(repository, safe='')}/blob/{quote(revision, safe='')}/{encoded_path}"
    repository_full_name = f"{organization}/{repository}"
    metadata = {
        "canonical_url": url,
        "content_sha256": _content_hash(text),
        "blob_sha": item.sha,
        "size": item.size,
        "content_type": content_type,
        "kind": "github_file",
        "branch": branch,
    }
    return _build_document(
        # The branch/path identity is stable across default-branch updates;
        # ``revision`` and the commit URL still make the indexed snapshot
        # auditable.  Historical commit indexing is intentionally out of scope.
        id=_stable_id("github", f"{repository_full_name}:{item.path}"),
        source="github",
        title=_github_title(item.path, text),
        url=url,
        text=text,
        version=branch,
        repository=repository_full_name,
        path=item.path,
        revision=revision,
        fetched_at=_now(),
        metadata=metadata,
    )


def _sync_github(
    store: Any,
    config: Mapping[str, Any],
    fetcher: _HttpFetcher,
    stats: dict[str, Any],
    *,
    max_document_chars: int,
) -> None:
    organization = str(config.get("organization") or "MetaX-MACA").strip()
    if organization.lower() != "metax-maca":
        _add_error(stats, url="", error="GitHub organization must be MetaX-MACA")
        return
    include_code = bool(config.get("include_code", False))
    max_files = _bounded_int(config.get("max_files_per_repo", 100), 100, 0, HARD_MAX_FILES_PER_REPO)
    raw_repositories = config.get("repositories")
    repositories: list[str] = []
    if raw_repositories is None:
        # Keep the default intentionally narrow.  A full organization crawl
        # requires an explicit empty list, which then uses bounded discovery.
        raw_repositories = ["maca-samples"]
    if isinstance(raw_repositories, Sequence) and not isinstance(raw_repositories, (str, bytes, bytearray)):
        if len(raw_repositories) == 0:
            repositories = _discover_github_repositories(organization, fetcher, stats)
        else:
            for raw in list(raw_repositories)[:HARD_MAX_REPOSITORIES]:
                try:
                    name = _normalise_repository(raw, organization)
                except _IngestionError as exc:
                    _add_error(stats, url="", error=exc, path=str(raw))
                    continue
                if name not in repositories:
                    repositories.append(name)
            if len(raw_repositories) > HARD_MAX_REPOSITORIES:
                _mark_truncated(stats, f"repository limit {HARD_MAX_REPOSITORIES}")
    else:
        _add_error(stats, url="", error="github.repositories must be a list")
        return
    if not repositories:
        _add_error(stats, url="", error="no GitHub repositories selected; set github.repositories explicitly")
        return

    token = os.environ.get("GITHUB_TOKEN", "")
    for repository in repositories:
        repo_root = f"{GITHUB_API_ROOT}/repos/{quote(organization, safe='')}/{quote(repository, safe='')}"
        try:
            repo_payload, _ = _github_json(fetcher, repo_root)
            if not isinstance(repo_payload, Mapping) or repo_payload.get("private") is True:
                raise _IngestionError("repository is not public")
            branch = str(repo_payload.get("default_branch") or "main")
            if not re.fullmatch(r"[^\s]{1,200}", branch):
                raise _IngestionError("invalid repository default branch")
            commit_url = f"{repo_root}/commits/{quote(branch, safe='')}"
            commit_payload, _ = _github_json(fetcher, commit_url)
            if not isinstance(commit_payload, Mapping):
                raise _IngestionError("GitHub commit response is invalid")
            revision = str(commit_payload.get("sha") or "")
            if not re.fullmatch(r"[0-9a-fA-F]{7,64}", revision):
                raise _IngestionError("GitHub commit response has no valid SHA")
            tree_url = f"{repo_root}/git/trees/{quote(revision, safe='')}?recursive=1"
            tree_payload, tree_response = _github_json(fetcher, tree_url)
            items, tree_truncated = _github_files(tree_payload, include_code=include_code, max_files=max_files)
            if tree_response.truncated:
                _mark_truncated(stats, f"GitHub response body limit for {repository}")
            if tree_truncated:
                _mark_truncated(stats, f"GitHub tree/file limit for {repository}")
            for item in items:
                stats["discovered"] += 1
                blob_url = f"{repo_root}/git/blobs/{quote(item.sha, safe='')}"
                try:
                    blob_payload, _ = _github_json(fetcher, blob_url)
                    if not isinstance(blob_payload, Mapping):
                        raise _IngestionError("GitHub blob response is invalid")
                    encoded = str(blob_payload.get("content") or "").replace("\n", "").replace("\r", "")
                    if str(blob_payload.get("encoding") or "").lower() != "base64":
                        raise _IngestionError("GitHub blob encoding is not base64")
                    try:
                        raw = base64.b64decode(encoded, validate=True)
                    except (binascii.Error, ValueError) as exc:
                        raise _IngestionError("GitHub blob contains invalid base64") from exc
                    if b"\x00" in raw[:8_192]:
                        raise _IngestionError("binary GitHub blob is not indexable")
                    text = raw.decode("utf-8", "replace")
                    if len(text) > max_document_chars:
                        _mark_truncated(stats, f"document size limit for {repository}/{item.path}")
                    document = _github_document(
                        organization=organization,
                        repository=repository,
                        branch=branch,
                        revision=revision,
                        item=item,
                        text=text,
                        content_type="text/plain; charset=utf-8",
                        max_document_chars=max_document_chars,
                    )
                    _store_document(store, document, stats)
                except Exception as exc:
                    _add_error(stats, url=blob_url, error=exc, path=item.path)
        except Exception as exc:
            _add_error(stats, url=repo_root, error=exc, path=repository)
            continue


def sync_sources(store: Any, config: dict[str, Any], source: str = "all") -> dict[str, Any]:
    """Synchronize selected public sources into ``store``.

    Args:
        store: An object implementing ``upsert(Document) -> bool``.
        config: ``official``, ``github`` and optional ``http`` mappings from
            :mod:`docs/CONTRACT.md`.
        source: ``official``, ``github`` or ``all``.

    The returned report is deliberately explicit.  ``indexed`` counts changed
    documents, ``unchanged`` counts hash-identical upserts, and ``failed``
    counts requests/items that could not be indexed.  Existing rows are never
    deleted, including when a source is partial.
    """

    if source not in {"official", "github", "docker", "all"}:
        raise ValueError("source must be official, github, docker, or all")
    if not isinstance(config, Mapping):
        raise TypeError("config must be a mapping")

    http_config = config.get("http") or {}
    if not isinstance(http_config, Mapping):
        http_config = {}
    timeout = _bounded_float(http_config.get("timeout", 20), 20.0, 0.1, 120.0)
    retries = _bounded_int(http_config.get("retries", 2), 2, 0, 5)
    rate_interval = _bounded_float(http_config.get("rate_limit", 0.05), 0.05, 0.0, 2.0)
    max_response_bytes = _bounded_int(
        http_config.get("max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES),
        DEFAULT_MAX_RESPONSE_BYTES,
        16 * 1024,
        HARD_MAX_RESPONSE_BYTES,
    )
    max_document_chars = _bounded_int(
        http_config.get("max_document_chars", DEFAULT_MAX_DOCUMENT_CHARS),
        DEFAULT_MAX_DOCUMENT_CHARS,
        1_024,
        HARD_MAX_DOCUMENT_CHARS,
    )

    official_config = config.get("official") or {}
    github_config = config.get("github") or {}
    if not isinstance(official_config, Mapping):
        official_config = {}
    if not isinstance(github_config, Mapping):
        github_config = {}

    # Only the known first-party host is allowed.  A seed cannot expand this
    # allowlist to an arbitrary public domain.
    official_hosts = {OFFICIAL_HOST}
    github_hosts = {"api.github.com"}
    client_headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json, text/html, text/plain;q=0.9, */*;q=0.1",
    }
    github_token = os.environ.get("GITHUB_TOKEN", "")
    # The token is held only in process memory and is never part of a URL,
    # report, document metadata, or exception text.
    github_headers = dict(client_headers)
    if github_token:
        github_headers["Authorization"] = f"Bearer {github_token}"

    reports = {
        "official": _new_stats("official"),
        "github": _new_stats("github"),
        "docker": _new_stats("docker"),
    }
    selected = [source] if source != "all" else [name for name in ("official", "github", "docker") if name in config]
    if not selected:
        raise ValueError("configure at least one source: official, github, docker")
    clients: list[Any] = []
    try:
        for name in selected:
            cfg = config.get(name) or {}
            hosts = github_hosts if name == "github" else official_hosts
            headers = github_headers if name == "github" else client_headers
            try:
                client = _new_client(timeout, headers)
                clients.append(client)
                fetcher = _HttpFetcher(
                    client,
                    allowed_hosts=hosts,
                    retries=retries,
                    max_response_bytes=max_response_bytes,
                    rate_limiter=_RateLimiter(rate_interval),
                    secret=github_token if name == "github" else None,
                )
                if name == "official":
                    _sync_official(store, cfg, fetcher, reports[name], max_document_chars=max_document_chars)
                elif name == "github":
                    _sync_github(store, cfg, fetcher, reports[name], max_document_chars=max_document_chars)
                else:
                    from .docker_source import sync_docker
                    sync_docker(store, cfg, fetcher, reports[name], max_document_chars=max_document_chars)
            except Exception as exc:
                _add_error(reports[name], url="", error=exc)
    finally:
        for client in clients:
            try:
                client.close()
            except Exception:
                pass

    for stats in reports.values():
        _finish_stats(stats)
    errors = [error | {"source": name} for name, stats in reports.items() for error in stats["errors"]]
    selected_reports = [reports[name] for name in selected]
    result: dict[str, Any] = {
        "source": source,
        "discovered": sum(item["discovered"] for item in selected_reports),
        "indexed": sum(item["indexed"] for item in selected_reports),
        "unchanged": sum(item["unchanged"] for item in selected_reports),
        "failed": sum(item["failed"] for item in selected_reports),
        "errors": errors,
        "truncated": any(item["truncated"] for item in selected_reports),
        "partial": any(item["partial"] for item in selected_reports),
        "coverage": {name: reports[name]["coverage"] for name in selected},
        "by_source": {name: reports[name] for name in selected},
    }
    return result
