"""Bounded ingestion for the public MetaX SoftNova Docker catalog.

The SoftNova page is a qiankun micro-frontend.  Its server rendered HTML is
only a shell; the Docker page calls the public JSON endpoint below instead::

    GET /softnova/api/v3/dlhub/docker_package_info/

This adapter intentionally reads catalog metadata only.  It never calls the
download or pull-command POST endpoints, executes a command, logs in to a
registry, or pulls an image.  The public list response usually contains an
image path but no registry host or pull command.  Those fields are retained as
empty/``not_exposed_by_public_list`` metadata until a source response exposes
them.

``sync_docker`` is kept in its own module so the dispatcher can import it
lazily without coupling this source to the HTML/GitHub crawler.  The fetcher
argument is deliberately duck typed: production uses the bounded fetcher from
``sources.py`` while unit tests can provide a small ``get(url)`` double.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Document


__all__ = ["sync_docker"]


DOCKER_HOST = "developer.metax-tech.com"
DOCKER_PAGE_URL = f"https://{DOCKER_HOST}/softnova/docker"
DOCKER_API_URL = f"https://{DOCKER_HOST}/softnova/api/v3/dlhub/docker_package_info/"

# These are source-specific bounds.  The HTTP body bound remains owned by the
# common fetcher, but a malformed page must not make this adapter unbounded.
HARD_MAX_SEEDS = 8
HARD_MAX_PAGES = 200
HARD_MAX_PAGE_SIZE = 100
HARD_MAX_ITEMS = 5_000
HARD_MAX_FIELD_CHARS = 4_096
HARD_MAX_METADATA_KEYS = 80
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 20
DEFAULT_MAX_ITEMS = 2_000

_SUCCESS_CODES = {None, 0, "0", "success", "SUCCESS"}
_SENSITIVE_KEYS = re.compile(r"(?:authorization|cookie|login|password|secret|token)", re.I)
_IMAGE_HOST_RE = re.compile(r"(?:^localhost$|[.:])")


class _DockerSourceError(RuntimeError):
    """An expected source/schema failure safe to expose in a sync report."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _stable_id(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()
    return f"docker:{digest}"


def _content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def _text(value: Any, *, limit: int = HARD_MAX_FIELD_CHARS) -> str:
    """Convert a scalar to bounded text without serialising arbitrary objects."""

    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        result = str(value).strip()
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        result = ", ".join(_text(item, limit=limit) for item in value)
    else:
        result = ""
    return result[:limit]


def _normalise_text(value: str, limit: int) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{4,}", "\n\n\n", value)
    return value.strip()[:limit]


def _canonical_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise _DockerSourceError("Docker source URL is empty")
    raw = url.strip()
    parts = urlsplit(raw)
    if (
        parts.scheme.lower() != "https"
        or (parts.hostname or "").lower() != DOCKER_HOST
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise _DockerSourceError("Docker source URL must use the official HTTPS host")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)), doseq=True)
    return urlunsplit(("https", DOCKER_HOST, parts.path or "/", query, ""))


def _safe_url(url: str, *, endpoint: bool = False) -> str:
    value = _canonical_url(url)
    path = urlsplit(value).path.rstrip("/")
    expected = "/softnova/api/v3/dlhub/docker_package_info" if endpoint else "/softnova/docker"
    if path != expected:
        raise _DockerSourceError(
            "Docker source URL must be the public SoftNova Docker page or metadata endpoint"
        )
    return value


def _set_query(url: str, params: Mapping[str, Any]) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key not in params]
    for key, value in params.items():
        if value is None or value == "":
            # Empty package_name is meaningful to the UI, but omitting it is
            # equivalent for this endpoint and keeps canonical URLs compact.
            continue
        query.append((key, str(value)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), ""))


def _add_error(stats: dict[str, Any], *, url: str, error: BaseException | str, path: str = "") -> None:
    message = str(error).strip() or error.__class__.__name__ if isinstance(error, BaseException) else str(error).strip()
    item: dict[str, str] = {"url": str(url), "error": message[:500]}
    if path:
        item["path"] = path[:500]
    stats.setdefault("errors", []).append(item)
    stats["failed"] = int(stats.get("failed", 0)) + 1
    stats["partial"] = True


def _mark_truncated(stats: dict[str, Any], reason: str) -> None:
    stats["truncated"] = True
    stats["partial"] = True
    reasons = stats.setdefault("truncation_reasons", [])
    if reason not in reasons:
        reasons.append(reason)


def _store_document(store: Any, document: Document, stats: dict[str, Any]) -> None:
    try:
        changed = bool(store.upsert(document))
    except Exception as exc:  # keep one bad store write from hiding other rows
        _add_error(stats, url=document.url, error=exc)
        return
    if changed:
        stats["indexed"] = int(stats.get("indexed", 0)) + 1
    else:
        stats["unchanged"] = int(stats.get("unchanged", 0)) + 1


def _response_json(response: Any) -> Mapping[str, Any]:
    status = int(getattr(response, "status_code", 200) or 0)
    if status < 200 or status >= 300:
        raise _DockerSourceError(f"HTTP {status}")
    if bool(getattr(response, "truncated", False)):
        raise _DockerSourceError("Docker metadata response exceeded the response bound")
    headers = {str(key).lower(): str(value) for key, value in getattr(response, "headers", {}).items()}
    content_type = headers.get("content-type", "").lower()
    if content_type and "json" not in content_type:
        raise _DockerSourceError(f"unsupported Docker metadata content type {content_type[:80]}")
    raw = getattr(response, "content", b"")
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    try:
        payload = json.loads(bytes(raw).decode("utf-8", "replace"))
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise _DockerSourceError("Docker metadata endpoint returned invalid JSON") from exc
    if not isinstance(payload, Mapping):
        raise _DockerSourceError("Docker metadata response is not an object")
    code = payload.get("code")
    if code not in _SUCCESS_CODES:
        message = _text(payload.get("message") or payload.get("error") or f"code={code}")
        raise _DockerSourceError(f"Docker metadata API error: {message or f'code={code}'}")
    return payload


def _field(item: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in item and item[name] not in (None, ""):
            return item[name]
    return ""


def _image_ref(item: Mapping[str, Any]) -> str:
    # ``path`` is the source's namespaced image reference (for example
    # public-ai-release/maca/vllm:tag); package_name is the short tag shown by
    # the UI.  Prefer a first-party explicit image/ref field when present.
    return _text(_field(item, "image_ref", "image", "docker_image", "path", "package_name"))


def _registry(item: Mapping[str, Any], image_ref: str) -> str:
    value = _text(_field(item, "registry", "registry_url", "registry_host", "registry_name"))
    if value:
        return value
    first = image_ref.split("/", 1)[0] if "/" in image_ref else ""
    # Docker treats the first component as a registry only when it resembles a
    # host (contains a dot/port or is localhost).  public-library/... is a
    # namespace, not enough evidence to invent a registry hostname.
    return first if _IMAGE_HOST_RE.search(first) else ""


def _tag(item: Mapping[str, Any], image_name: str, image_ref: str) -> str:
    value = _text(_field(item, "tag", "image_tag"))
    if value:
        return value
    candidate = image_name or image_ref.rsplit("/", 1)[-1]
    if "@" in candidate:
        return candidate.split("@", 1)[1]
    tail = candidate.rsplit("/", 1)[-1]
    if ":" in tail:
        return tail.rsplit(":", 1)[1]
    return ""


def _safe_raw_metadata(item: Mapping[str, Any]) -> dict[str, Any]:
    """Retain bounded scalar/list fields while dropping credential-shaped keys."""

    result: dict[str, Any] = {}
    for key, value in item.items():
        name = str(key)
        if _SENSITIVE_KEYS.search(name) or len(result) >= HARD_MAX_METADATA_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[name] = value if value is None else _text(value)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            result[name] = [_text(part, limit=512) for part in list(value)[:32]]
    return result


def _pull_command(item: Mapping[str, Any], image_ref: str) -> tuple[str, str]:
    value = _text(_field(item, "pull_cmd", "pull_command", "docker_pull", "docker_pull_command"))
    if value:
        return value, "source_record"
    # A response can expose a namespaced path but no registry hostname.  Keep a
    # syntactically useful command using that path; metadata tells callers that
    # the registry/pull command was inferred, so it is never mistaken for a
    # verified executable command.  We do not execute it.
    if image_ref:
        return f"docker pull {image_ref}"[:HARD_MAX_FIELD_CHARS], "inferred_from_path"
    return "", "not_exposed_by_public_list"


def _document_from_item(
    item: Mapping[str, Any],
    *,
    page_url: str,
    max_document_chars: int,
) -> Document:
    record_id = _text(_field(item, "_id", "id", "package_id"))
    package_name = _text(_field(item, "package_name", "image_name", "name"))
    image_ref = _image_ref(item)
    if not (package_name or image_ref):
        raise _DockerSourceError("Docker record has no image name or path")
    # The public API can reuse one `_id` for multiple OS/image variants.  The
    # source path plus package name identifies the actual image; retain the
    # record id as provenance rather than trusting it as a unique key.
    identity = (
        f"{image_ref}|{package_name}|{_text(_field(item, 'chip_name'))}|"
        f"{_text(_field(item, 'deliver_type'))}"
    )
    if not (image_ref or package_name):
        identity = record_id
    registry = _registry(item, image_ref)
    image_name = package_name or image_ref
    tag = _tag(item, image_name, image_ref)
    pull_command, pull_command_status = _pull_command(item, image_ref)
    chip_name = _text(_field(item, "chip_name", "chip", "chip_series"))
    package_kind = _text(_field(item, "package_kind", "kind"))
    dimension = _text(_field(item, "dimension"))
    deliver_type = _text(_field(item, "deliver_type", "deliverType"))
    framework = _text(_field(item, "framework", "ai_frame", "frame"))
    framework_version = _text(_field(item, "framework_version", "frame_version", "ai_frame_version"))
    maca_version = _text(_field(item, "maca_main_version", "maca_version", "sdk_version", "compatible_maca"))
    python_version = _text(_field(item, "python_version", "compatible_python"))
    pytorch_version = _text(_field(item, "pytorch_version", "compatible_pytorch"))
    updated_at = _text(_field(item, "updated_at", "update_time", "modified_at"))
    created_at = _text(_field(item, "created_at", "create_time"))
    system = _text(_field(item, "system"))
    system_version = _text(_field(item, "system_version"))
    arch = _text(_field(item, "arch", "architecture"))
    source_path = _text(_field(item, "path"))
    container_path = _text(_field(item, "container_path"))

    lines = [f"# Docker image: {image_name or image_ref}"]
    fields = (
        ("Image reference", image_ref),
        ("Registry", registry),
        ("Tag", tag),
        ("Pull command", pull_command),
        ("Chip", chip_name),
        ("Package kind", package_kind),
        ("Dimension", dimension),
        ("Deliver type", deliver_type),
        ("Framework", framework),
        ("Framework version", framework_version),
        ("MXMACA version", maca_version),
        ("Python version", python_version),
        ("PyTorch version", pytorch_version),
        ("Architecture", arch),
        ("Operating system", f"{system} {system_version}".strip()),
        ("Updated at", updated_at),
        ("Created at", created_at),
        ("Container path", container_path),
        ("Source path", source_path),
    )
    lines.extend(f"- {label}: {value}" for label, value in fields if value)
    text = _normalise_text("\n".join(lines), max_document_chars)
    metadata: dict[str, Any] = {
        "kind": "docker_image",
        "canonical_url": page_url,
        "source_page_url": page_url,
        "source_api": DOCKER_API_URL,
        "source_record_id": record_id,
        "image_name": image_name,
        "image_ref": image_ref,
        "tag": tag,
        "registry": registry,
        "pull_command": pull_command,
        "pull_command_status": pull_command_status,
        "chip_name": chip_name,
        "package_kind": package_kind,
        "dimension": dimension,
        "deliver_type": deliver_type,
        "framework": framework,
        "framework_version": framework_version,
        "maca_version": maca_version,
        "python_version": python_version,
        "pytorch_version": pytorch_version,
        "updated_at": updated_at,
        "created_at": created_at,
        "content_sha256": _content_hash(text),
        "raw": _safe_raw_metadata(item),
    }
    if source_path:
        metadata["path"] = source_path
    version = maca_version or framework_version or updated_at
    return Document(
        id=_stable_id(identity),
        source="docker",
        title=(package_name or image_ref or record_id)[:1_000],
        url=page_url,
        text=text,
        version=version[:HARD_MAX_FIELD_CHARS],
        repository="",
        path=source_path,
        revision="",
        fetched_at=_now(),
        metadata=metadata,
    )


def _seed_specs(config: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Return ``(endpoint, page_url)`` pairs from page or API seed URLs."""

    raw = config.get("seeds")
    if raw is None:
        raw = [config.get("url") or DOCKER_PAGE_URL]
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, Sequence) or isinstance(raw, (bytes, bytearray)):
        raise _DockerSourceError("docker.seeds must be a list")
    seeds = list(raw)
    if len(seeds) > HARD_MAX_SEEDS:
        seeds = seeds[:HARD_MAX_SEEDS]
    endpoint_config = _text(config.get("endpoint"))
    pairs: list[tuple[str, str]] = []
    for raw_seed in seeds:
        seed = _canonical_url(str(raw_seed))
        path = urlsplit(seed).path.rstrip("/")
        if path == "/softnova/docker":
            endpoint = endpoint_config or DOCKER_API_URL
            endpoint = _safe_url(endpoint, endpoint=True)
            page_url = seed
        elif path == "/softnova/api/v3/dlhub/docker_package_info":
            endpoint = _safe_url(seed, endpoint=True)
            page_url = DOCKER_PAGE_URL
        else:
            raise _DockerSourceError("docker seed must be the official SoftNova Docker page or API endpoint")
        pairs.append((endpoint, page_url))
    if not pairs:
        raise _DockerSourceError("no Docker seeds configured")
    return pairs


def _base_params(config: Mapping[str, Any], page_url: str) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key, value in parse_qsl(urlsplit(page_url).query, keep_blank_values=True):
        params.setdefault(key, value)
    overrides = config.get("params")
    if overrides is not None:
        if not isinstance(overrides, Mapping):
            raise _DockerSourceError("docker.params must be an object")
        params.update({str(key): value for key, value in overrides.items()})
    for key in ("chip_name", "package_name", "package_kind", "dimension", "deliver_type", "ai_frame", "frame_version", "arch", "system"):
        if key in config and config[key] is not None:
            params[key] = config[key]
    params.setdefault("chip_name", "曦云C500系列")
    params.setdefault("package_kind", "MXMACA")
    params.setdefault("dimension", "docker")
    params.setdefault("deliver_type", "分层包")
    return params


def sync_docker(
    store: Any,
    config: Mapping[str, Any],
    fetcher: Any,
    stats: dict[str, Any],
    *,
    max_document_chars: int,
) -> None:
    """Synchronize bounded public Docker metadata into ``store``.

    The function mutates the caller-owned source report and returns ``None``;
    this matches the official/GitHub adapters used by ``sync_sources``.
    """

    if not isinstance(config, Mapping):
        _add_error(stats, url="", error="docker config must be an object")
        return
    try:
        specs = _seed_specs(config)
        page_size = _bounded_int(config.get("page_size", config.get("size", DEFAULT_PAGE_SIZE)), DEFAULT_PAGE_SIZE, 1, HARD_MAX_PAGE_SIZE)
        max_pages = _bounded_int(config.get("max_pages", DEFAULT_MAX_PAGES), DEFAULT_MAX_PAGES, 1, HARD_MAX_PAGES)
        max_items = _bounded_int(config.get("max_items", DEFAULT_MAX_ITEMS), DEFAULT_MAX_ITEMS, 1, HARD_MAX_ITEMS)
        max_document_chars = max(1, int(max_document_chars))
    except Exception as exc:
        _add_error(stats, url="", error=exc)
        return

    discovered = 0
    seen: set[str] = set()
    pages_read = 0
    item_limit_reached = False
    page_limit_reached = False
    for endpoint, page_url in specs:
        if pages_read >= max_pages or discovered >= max_items:
            break
        params = _base_params(config, page_url)
        page = 1
        seed_exhausted = False
        total = 0
        seed_unique_before = len(seen)
        while page <= max_pages and pages_read < max_pages and discovered < max_items:
            request_params = dict(params)
            request_params["page"] = page
            request_params["size"] = min(page_size, max_items - discovered)
            request_url = _set_query(endpoint, request_params)
            pages_read += 1
            try:
                response = fetcher.get(request_url)
                payload = _response_json(response)
                data = payload.get("data")
                if not isinstance(data, Mapping):
                    raise _DockerSourceError("Docker metadata response has no data object")
                results = data.get("results")
                if not isinstance(results, list):
                    raise _DockerSourceError("Docker metadata response has no results list")
                total = _bounded_int(data.get("total"), 0, 0, HARD_MAX_ITEMS)
                total_page = _bounded_int(data.get("total_page"), 0, 0, HARD_MAX_PAGES)
                for index, item in enumerate(results):
                    if discovered >= max_items:
                        break
                    discovered += 1
                    stats["discovered"] = int(stats.get("discovered", 0)) + 1
                    item_path = str(item.get("_id") or item.get("package_name") or index) if isinstance(item, Mapping) else str(index)
                    try:
                        if not isinstance(item, Mapping):
                            raise _DockerSourceError("Docker result is not an object")
                        document = _document_from_item(item, page_url=page_url, max_document_chars=max_document_chars)
                        if document.id in seen:
                            continue
                        seen.add(document.id)
                        _store_document(store, document, stats)
                    except Exception as exc:
                        _add_error(stats, url=request_url, error=exc, path=item_path)
                # Stop on an empty page, the server's total_page, or when all
                # reported records have been seen.  If the endpoint ignores
                # pagination, ``seen``/max_items still provide a hard bound.
                if not results or (total_page and page >= total_page) or (total and page * page_size >= total):
                    seed_exhausted = True
                    break
                if len(results) < page_size and not total:
                    seed_exhausted = True
                    break
                page += 1
            except Exception as exc:
                _add_error(stats, url=request_url, error=exc)
                seed_exhausted = True
                break
        if not seed_exhausted and (page > max_pages or pages_read >= max_pages):
            page_limit_reached = True
        seed_unique = len(seen) - seed_unique_before
        if seed_exhausted and total and seed_unique < min(total, max_items - (discovered - seed_unique)):
            _mark_truncated(
                stats,
                f"Docker pagination returned {seed_unique} unique records of {total}",
            )
        if discovered >= max_items and (not total or total > max_items):
            item_limit_reached = True
    if page_limit_reached:
        _mark_truncated(stats, f"Docker page limit {max_pages}")
    if item_limit_reached:
        _mark_truncated(stats, f"Docker item limit {max_items}")
