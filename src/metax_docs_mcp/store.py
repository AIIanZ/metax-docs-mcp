"""SQLite-backed offline index for MetaX documentation.

The store is intentionally self-contained.  Source adapters only need to
construct :class:`~metax_docs_mcp.models.Document` values and call
``upsert``; all schema creation, indexing, section chunking and retrieval are
handled here.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Document


# These bounds keep a malformed source response from consuming unbounded local
# memory or producing an unexpectedly large MCP response.  The document limit
# intentionally matches the public contract's default.
MAX_ID_CHARS = 512
MAX_QUERY_CHARS = 1024
MAX_FIELD_CHARS = 16_384
MAX_TEXT_CHARS = 10_000_000
MAX_METADATA_CHARS = 1_000_000
MAX_SEARCH_LIMIT = 100
MAX_SEARCH_CANDIDATES = 5_000
MAX_DOCUMENT_LIMIT = 12_000
MAX_SECTION_CHARS = 1_024
MAX_SECTION_ENTRIES = 1_000

_MARKDOWN_HEADING = re.compile(r"^(?P<marks>#{1,6})[ \t]+(?P<title>.*?)[ \t]*#*[ \t]*$")
_SETEXT_HEADING = re.compile(r"^[ \t]*(?:=+|-+)[ \t]*$")
_HTML_HEADING = re.compile(
    r"^[ \t]*<h(?P<level>[1-6])(?:\s[^>]*)?>(?P<title>.*?)</h[1-6]>[ \t]*$",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _utc_now() -> str:
    """Return a sortable UTC timestamp with enough precision for refreshes."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _normalise(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _slug(value: str) -> str:
    """Create a stable, human-readable anchor for a section heading."""

    value = _normalise(_TAG_RE.sub("", value)).strip()
    # Keep CJK characters and Unicode letters/numbers.  Punctuation becomes a
    # separator, which also makes anchors predictable for technical headings.
    chars: list[str] = []
    pending_separator = False
    for char in value:
        if char.isalnum() or char == "_":
            if pending_separator and chars:
                chars.append("-")
            chars.append(char)
            pending_separator = False
        elif char in "-":
            pending_separator = True
        else:
            pending_separator = True
    return "".join(chars).strip("-") or "section"


def _safe_json(metadata: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            dict(metadata), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise TypeError("metadata must be JSON serializable") from exc
    if len(encoded) > MAX_METADATA_CHARS:
        raise ValueError(f"metadata exceeds {MAX_METADATA_CHARS} characters")
    return encoded


def _json_object(encoded: str) -> dict[str, Any]:
    try:
        value = json.loads(encoded)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _validate_text(value: Any, field: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if len(value) > max_chars:
        raise ValueError(f"{field} exceeds {max_chars} characters")
    return value


def _validate_optional_filter(value: Any, field: str) -> str | None:
    if value is None:
        return None
    value = _validate_text(value, field, MAX_FIELD_CHARS)
    return value


def _bounded_limit(value: Any, *, default: int, maximum: int) -> int:
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("limit must be an integer")
    if value < 1:
        raise ValueError("limit must be at least 1")
    return min(value, maximum)


def _bounded_offset(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("offset must be an integer")
    if value < 0:
        raise ValueError("offset must be non-negative")
    # A very large offset is harmless in SQLite but can be a sign of an
    # accidental unbounded request.  It is still useful to allow offsets past
    # the end of a document, so use a generous bound rather than its length.
    return min(value, MAX_TEXT_CHARS)


def _query_terms(query: str) -> list[str]:
    """Split a query without exposing FTS5's query language.

    Keeping CJK runs intact lets the Python fallback match two-character
    Chinese terms that the bundled trigram tokenizer cannot index.  Latin
    identifiers preserve underscores and hyphenated tool names such as
    ``mx-smi``.
    """

    normalised = _normalise(query)
    terms = re.findall(r"[a-z0-9]+(?:[_-][a-z0-9]+)*|[\u3400-\u9fff]+", normalised)
    if not terms:
        compact = "".join(normalised.split())
        return [compact] if compact else []
    return terms


def _snippet(text: str, terms: list[str], width: int = 320) -> str:
    """Return a bounded snippet centered on the first matching term."""

    if not text:
        return ""
    normalised = _normalise(text)
    positions = [normalised.find(term) for term in terms if term]
    positions = [position for position in positions if position >= 0]
    start = min(positions) if positions else 0
    half = max(1, width // 2)
    left = max(0, start - half)
    right = min(len(text), left + width)
    if right - left < width:
        left = max(0, right - width)
    snippet = " ".join(text[left:right].split())
    if left > 0:
        snippet = "…" + snippet
    if right < len(text):
        snippet += "…"
    return snippet


def _parse_sections(text: str) -> list[dict[str, Any]]:
    """Split Markdown/HTML-ish text into hierarchical section chunks.

    A section owns the content after its heading until the next heading of the
    same or a higher level.  Consequently a top-level section includes its
    nested subsections, which makes ``get_section`` useful for reading a whole
    API/topic section while the stored offsets still allow precise navigation.
    """

    lines = text.splitlines(keepends=True)
    headings: list[dict[str, Any]] = []
    cursor = 0
    fence_character: str | None = None
    for index, line in enumerate(lines):
        line_without_newline = line.rstrip("\r\n")
        # Headings and Setext markers inside fenced code are source content,
        # not document structure.  This matters for shell comments (``#``),
        # YAML separators and examples containing Markdown of their own.
        fence = re.match(r"^[ \t]*(?P<marker>`{3,}|~{3,})", line_without_newline)
        if fence:
            marker = fence.group("marker")[0]
            if fence_character is None:
                fence_character = marker
            elif marker == fence_character:
                fence_character = None
            cursor += len(line)
            continue
        if fence_character is not None:
            cursor += len(line)
            continue
        markdown = _MARKDOWN_HEADING.match(line_without_newline)
        html = _HTML_HEADING.match(line_without_newline)
        title: str | None = None
        level = 0
        heading_end = cursor + len(line)
        if markdown:
            title = markdown.group("title").strip()
            level = len(markdown.group("marks"))
        elif html:
            title = _TAG_RE.sub("", html.group("title")).strip()
            level = int(html.group("level"))
        else:
            # Support the common Setext Markdown form: a non-empty line
            # followed by ``===`` or ``---``.
            if index > 0 and _SETEXT_HEADING.match(line_without_newline):
                previous = lines[index - 1].rstrip("\r\n").strip()
                if previous:
                    previous_start = cursor - len(lines[index - 1])
                    headings.append(
                        {
                            "start": previous_start,
                            "heading_end": heading_end,
                            "level": 1 if line_without_newline.lstrip().startswith("=") else 2,
                            "heading": previous,
                        }
                    )
            cursor += len(line)
            continue
        if title:
            headings.append(
                {
                    "start": cursor,
                    "heading_end": heading_end,
                    "level": level,
                    "heading": title,
                }
            )
        cursor += len(line)

    if not headings:
        return [
            {
                "ordinal": 0,
                "heading": "",
                "level": 0,
                "anchor": "root",
                "section_path": "root",
                "start_offset": 0,
                "end_offset": len(text),
                "content": text.strip(),
            }
        ]

    # A heading can be represented once only.  Setext parsing sees the same
    # physical line as the next iteration; remove accidental duplicates while
    # retaining source order.
    deduped: list[dict[str, Any]] = []
    seen_starts: set[int] = set()
    for heading in sorted(headings, key=lambda item: (item["start"], item["heading_end"])):
        if heading["start"] in seen_starts:
            continue
        seen_starts.add(heading["start"])
        deduped.append(heading)
    headings = deduped

    sections: list[dict[str, Any]] = []
    if headings[0]["start"] > 0 and text[: headings[0]["start"]].strip():
        sections.append(
            {
                "ordinal": 0,
                "heading": "",
                "level": 0,
                "anchor": "root",
                "section_path": "root",
                "start_offset": 0,
                "end_offset": headings[0]["start"],
                "content": text[: headings[0]["start"]].strip(),
            }
        )

    path_stack: list[tuple[int, str]] = []
    used_anchors: dict[str, int] = {}
    for heading_index, heading in enumerate(headings):
        level = int(heading["level"])
        title = str(heading["heading"]).strip()
        while path_stack and path_stack[-1][0] >= level:
            path_stack.pop()
        path_stack.append((level, title))
        section_path = " > ".join(item[1] for item in path_stack)
        anchor = _slug(title)
        used_anchors[anchor] = used_anchors.get(anchor, 0) + 1
        if used_anchors[anchor] > 1:
            anchor = f"{anchor}-{used_anchors[anchor]}"

        end = len(text)
        for following in headings[heading_index + 1 :]:
            if int(following["level"]) <= level:
                end = int(following["start"])
                break
        content_start = int(heading["heading_end"])
        content = text[content_start:end].strip()
        sections.append(
            {
                "ordinal": len(sections),
                "heading": title,
                "level": level,
                "anchor": anchor,
                "section_path": section_path,
                "start_offset": content_start,
                "end_offset": end,
                "content": content,
            }
        )
    return sections


class Store:
    """A small transactional SQLite document and section index."""

    def __init__(self, db_path: str | Path, readonly: bool = False):
        """Open an index.

        ``readonly=True`` is useful for MCP/CLI query processes: it refuses to
        create or migrate a database and therefore keeps read-only interfaces
        from mutating an accidentally misspelled path.  The public contract
        only requires ``Store(db_path)``; the optional flag is backwards
        compatible for ingestion callers.
        """

        self.db_path = str(db_path)
        if readonly and self.db_path == ":memory:":
            raise ValueError("readonly mode requires a filesystem database")
        if self.db_path != ":memory:":
            path = Path(self.db_path).expanduser()
            if readonly:
                if not path.is_file():
                    raise FileNotFoundError(f"index database does not exist: {path}")
                path = path.resolve()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
            self.db_path = str(path)
        if readonly:
            self._conn = sqlite3.connect(
                f"{Path(self.db_path).as_uri()}?mode=ro", uri=True, check_same_thread=False
            )
        else:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if readonly:
            try:
                self._conn.execute("SELECT 1 FROM documents LIMIT 0")
                self._conn.execute("SELECT 1 FROM sections LIMIT 0")
                self._conn.execute("SELECT 1 FROM documents_fts LIMIT 0")
            except sqlite3.DatabaseError as exc:
                self._conn.close()
                raise RuntimeError("index database is not initialized") from exc
        else:
            self._initialise_schema()

    def _initialise_schema(self) -> None:
        try:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    text TEXT NOT NULL,
                    version TEXT NOT NULL DEFAULT '',
                    repository TEXT NOT NULL DEFAULT '',
                    path TEXT NOT NULL DEFAULT '',
                    revision TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    content_hash TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_documents_source
                    ON documents(source);
                CREATE INDEX IF NOT EXISTS idx_documents_repository
                    ON documents(repository);
                CREATE INDEX IF NOT EXISTS idx_documents_version
                    ON documents(version);
                CREATE TABLE IF NOT EXISTS sections (
                    document_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    heading TEXT NOT NULL,
                    level INTEGER NOT NULL,
                    anchor TEXT NOT NULL,
                    section_path TEXT NOT NULL,
                    start_offset INTEGER NOT NULL,
                    end_offset INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    PRIMARY KEY(document_id, ordinal),
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_sections_anchor
                    ON sections(document_id, anchor);
                """
            )
            # The trigram tokenizer gives useful indexing for CJK text and
            # punctuation-bearing identifiers.  Search still performs a safe
            # Python fallback for terms shorter than three characters.
            self._conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                    document_id UNINDEXED,
                    title,
                    text,
                    tokenize='trigram'
                )
                """
            )
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower() or "no such module" in str(exc).lower():
                raise RuntimeError("SQLite FTS5 support is required") from exc
            raise

    @staticmethod
    def _validate_document(document: Document) -> tuple[Document, str, str]:
        if not isinstance(document, Document):
            raise TypeError("document must be a Document")
        identifier = _validate_text(document.id, "id", MAX_ID_CHARS).strip()
        if not identifier:
            raise ValueError("id must not be empty")
        source = _validate_text(document.source, "source", 32).strip().lower()
        if source not in {"official", "github", "docker"}:
            raise ValueError("source must be 'official', 'github', or 'docker'")
        title = _validate_text(document.title, "title", MAX_FIELD_CHARS)
        url = _validate_text(document.url, "url", MAX_FIELD_CHARS)
        if not url.strip():
            raise ValueError("url must not be empty")
        text = _validate_text(document.text, "text", MAX_TEXT_CHARS)
        version = _validate_text(document.version, "version", MAX_FIELD_CHARS)
        repository = _validate_text(document.repository, "repository", MAX_FIELD_CHARS)
        path = _validate_text(document.path, "path", MAX_FIELD_CHARS)
        revision = _validate_text(document.revision, "revision", MAX_FIELD_CHARS)
        fetched_at = _validate_text(document.fetched_at, "fetched_at", 128)
        if not isinstance(document.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        metadata = dict(document.metadata)
        normalised = Document(
            id=identifier,
            source=source,
            title=title,
            url=url,
            text=text,
            version=version,
            repository=repository,
            path=path,
            revision=revision,
            fetched_at=fetched_at,
            metadata=metadata,
        )
        metadata_json = _safe_json(metadata)
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return normalised, metadata_json, content_hash

    def upsert(self, doc: Document) -> bool:
        """Insert or update a document, returning whether indexed content changed.

        ``fetched_at`` is deliberately excluded from the change comparison.  A
        repeated fetch therefore returns ``False`` while still refreshing the
        timestamp, which lets callers distinguish unchanged content from a
        newly indexed revision without losing freshness information.
        """

        document, metadata_json, content_hash = self._validate_document(doc)
        now = _utc_now()
        row = self._conn.execute(
            """
            SELECT source, title, url, text, version, repository, path,
                   revision, metadata_json, content_hash
            FROM documents WHERE id = ?
            """,
            (document.id,),
        ).fetchone()
        values = (
            document.source,
            document.title,
            document.url,
            document.text,
            document.version,
            document.repository,
            document.path,
            document.revision,
            metadata_json,
            content_hash,
        )
        changed = row is None or tuple(row) != values
        fetched_at = document.fetched_at or now

        with self._conn:
            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO documents
                        (id, source, title, url, text, version, repository, path,
                         revision, fetched_at, metadata_json, content_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (document.id, *values[:8], fetched_at, metadata_json, content_hash),
                )
                self._replace_sections(document.id, document.text)
                self._replace_fts(document.id, document.title, document.text)
            elif changed:
                self._conn.execute(
                    """
                    UPDATE documents SET source=?, title=?, url=?, text=?,
                        version=?, repository=?, path=?, revision=?, fetched_at=?,
                        metadata_json=?, content_hash=? WHERE id=?
                    """,
                    (*values[:8], fetched_at, metadata_json, content_hash, document.id),
                )
                self._replace_sections(document.id, document.text)
                self._replace_fts(document.id, document.title, document.text)
            else:
                # Always refresh on an unchanged fetch.  The generated value is
                # intentionally independent of a stale source supplied value.
                self._conn.execute(
                    "UPDATE documents SET fetched_at=? WHERE id=?",
                    (now, document.id),
                )
        return changed

    def _replace_sections(self, document_id: str, text: str) -> None:
        sections = _parse_sections(text)
        self._conn.execute("DELETE FROM sections WHERE document_id=?", (document_id,))
        self._conn.executemany(
            """
            INSERT INTO sections
                (document_id, ordinal, heading, level, anchor, section_path,
                 start_offset, end_offset, content)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    document_id,
                    int(section["ordinal"]),
                    str(section["heading"]),
                    int(section["level"]),
                    str(section["anchor"]),
                    str(section["section_path"]),
                    int(section["start_offset"]),
                    int(section["end_offset"]),
                    str(section["content"]),
                )
                for section in sections
            ],
        )

    def _replace_fts(self, document_id: str, title: str, text: str) -> None:
        self._conn.execute("DELETE FROM documents_fts WHERE document_id=?", (document_id,))
        self._conn.execute(
            "INSERT INTO documents_fts(document_id, title, text) VALUES (?, ?, ?)",
            (document_id, title, text),
        )

    @staticmethod
    def _validate_search_filters(
        source: str | None, repository: str | None, version: str | None
    ) -> tuple[str | None, str | None, str | None]:
        source = _validate_optional_filter(source, "source")
        if source is not None:
            source = source.strip().lower()
            if source not in {"official", "github", "docker"}:
                raise ValueError("source must be 'official', 'github', or 'docker'")
        repository = _validate_optional_filter(repository, "repository")
        version = _validate_optional_filter(version, "version")
        return source, repository, version

    def search(
        self,
        query: str,
        source: str | None = None,
        repository: str | None = None,
        version: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search documents with safe hybrid FTS and Unicode substring matching."""

        query = _validate_text(query, "query", MAX_QUERY_CHARS).strip()
        if not query:
            raise ValueError("query must not be empty")
        source, repository, version = self._validate_search_filters(source, repository, version)
        limit = _bounded_limit(limit, default=10, maximum=MAX_SEARCH_LIMIT)
        terms = _query_terms(query)
        normalised_query = _normalise(query)

        clauses: list[str] = []
        parameters: list[str] = []
        if source is not None:
            clauses.append("source = ?")
            parameters.append(source)
        if repository is not None:
            clauses.append("repository = ?")
            parameters.append(repository)
        if version is not None:
            clauses.append("version = ?")
            parameters.append(version)

        # Use a quoted FTS5 expression made exclusively from parsed terms.  A
        # caller can therefore search for punctuation-bearing identifiers or
        # quote characters without gaining access to MATCH's operators.  The
        # SQL fallback handles terms shorter than a trigram (notably common
        # two-character Chinese words).
        fts_terms = [term for term in terms if len(term) >= 3]
        short_terms = [term for term in terms if len(term) < 3]
        fts_ids: list[str] | None = None
        if fts_terms:
            fts_expression = " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in fts_terms)
            fts_filter_clauses = ["documents_fts MATCH ?"]
            fts_parameters: list[Any] = [fts_expression]
            if source is not None:
                fts_filter_clauses.append("d.source = ?")
                fts_parameters.append(source)
            if repository is not None:
                fts_filter_clauses.append("d.repository = ?")
                fts_parameters.append(repository)
            if version is not None:
                fts_filter_clauses.append("d.version = ?")
                fts_parameters.append(version)
            for term in short_terms:
                fts_filter_clauses.append("(instr(lower(d.title), ?) > 0 OR instr(lower(d.text), ?) > 0)")
                fts_parameters.extend((term, term))
            fts_where = " AND ".join(fts_filter_clauses)
            # Keep the FTS table's real name in MATCH.  Some SQLite builds do
            # not resolve MATCH against an alias (``f MATCH ?``), and silently
            # falling back would turn a large index into a truncated scan.
            fts_rows = self._conn.execute(
                f"""
                SELECT documents_fts.document_id
                FROM documents_fts
                JOIN documents AS d ON d.id = documents_fts.document_id
                WHERE {fts_where}
                ORDER BY rank
                LIMIT ?
                """,
                (*fts_parameters, MAX_SEARCH_CANDIDATES),
            ).fetchall()
            fts_ids = [str(row["document_id"]) for row in fts_rows]
            if fts_ids == []:
                return []
            if fts_ids is not None:
                # Never build an unbounded IN expression if a broad term hits
                # many documents.  The FTS result is already relevance ordered.
                placeholders = ",".join("?" for _ in fts_ids)
                clauses.append(f"id IN ({placeholders})")
                parameters.extend(fts_ids)

        for term in short_terms:
            clauses.append("(instr(lower(title), ?) > 0 OR instr(lower(text), ?) > 0)")
            parameters.extend((term, term))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"""
            SELECT id, source, title, url, text, version, repository, path,
                   revision, fetched_at, metadata_json, content_hash
            FROM documents {where}
            LIMIT ?
            """,
            [*parameters, MAX_SEARCH_CANDIDATES],
        ).fetchall()

        scored: list[tuple[float, sqlite3.Row, str]] = []
        for row in rows:
            haystack = _normalise(
                "\n".join(
                    (
                        str(row["title"]),
                        str(row["text"]),
                        str(row["path"]),
                        str(row["repository"]),
                    )
                )
            )
            # Every meaningful term must occur.  This mirrors an AND query in
            # FTS while preserving two-character Chinese matches.
            if terms and not all(term in haystack for term in terms):
                continue
            if not terms and normalised_query not in haystack:
                continue
            score = 0.0
            for term in terms or [normalised_query]:
                occurrences = haystack.count(term)
                score += min(occurrences, 20) * 1.0
                if _normalise(str(row["title"])).find(term) >= 0:
                    score += 4.0
            if normalised_query and normalised_query in haystack:
                score += 2.0
            # Keep deterministic ordering for equal scores and prefer newer
            # fetched entries only when relevance is otherwise identical.
            score += min(len(str(row["text"])), MAX_TEXT_CHARS) / MAX_TEXT_CHARS * 0.001
            snippet_terms = terms or [normalised_query]
            snippet_source = str(row["text"])
            if not any(term and term in _normalise(snippet_source) for term in snippet_terms):
                # A title/path/repository match still needs a useful hit
                # segment; otherwise the snippet would misleadingly show the
                # beginning of unrelated document text.
                snippet_source = f"{row['title']}\n{row['path']}\n{row['repository']}\n{snippet_source}"
            scored.append((score, row, _snippet(snippet_source, snippet_terms)))

        scored.sort(key=lambda item: (-item[0], str(item[1]["id"])))
        results: list[dict[str, Any]] = []
        for score, row, snippet in scored[:limit]:
            results.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "url": row["url"],
                    "source": row["source"],
                    "version": row["version"],
                    "repository": row["repository"],
                    "revision": row["revision"],
                    "snippet": snippet,
                    "score": round(float(score), 6),
                    "fetched_at": row["fetched_at"],
                }
            )
        return results

    @staticmethod
    def _document_response(row: sqlite3.Row, text: str, *, offset: int, limit: int) -> dict[str, Any]:
        chunk = text[offset : offset + limit]
        truncated = offset + len(chunk) < len(text)
        return {
            "id": row["id"],
            "source": row["source"],
            "title": row["title"],
            "url": row["url"],
            "text": chunk,
            "offset": offset,
            "limit": limit,
            "total_length": len(text),
            "truncated": truncated,
            "next_offset": offset + len(chunk) if truncated else None,
            "version": row["version"],
            "repository": row["repository"],
            "path": row["path"],
            "revision": row["revision"],
            "fetched_at": row["fetched_at"],
            "metadata": _json_object(row["metadata_json"]),
            "content_hash": row["content_hash"],
        }

    def get_document(
        self, document_id: str, offset: int = 0, limit: int = MAX_DOCUMENT_LIMIT
    ) -> dict[str, Any] | None:
        document_id = _validate_text(document_id, "document_id", MAX_ID_CHARS).strip()
        if not document_id:
            raise ValueError("document_id must not be empty")
        offset = _bounded_offset(offset)
        limit = _bounded_limit(limit, default=MAX_DOCUMENT_LIMIT, maximum=MAX_DOCUMENT_LIMIT)
        row = self._conn.execute(
            """
            SELECT id, source, title, url, text, version, repository, path,
                   revision, fetched_at, metadata_json, content_hash
            FROM documents WHERE id=?
            """,
            (document_id,),
        ).fetchone()
        if row is None:
            return None
        response = self._document_response(row, str(row["text"]), offset=offset, limit=limit)
        section_rows = self._conn.execute(
            """
            SELECT ordinal, heading, level, anchor, section_path,
                   start_offset, end_offset
            FROM sections WHERE document_id=? ORDER BY ordinal LIMIT ?
            """,
            (document_id, MAX_SECTION_ENTRIES),
        ).fetchall()
        response["sections"] = [
            {
                "ordinal": section["ordinal"],
                "heading": section["heading"],
                "level": section["level"],
                "anchor": section["anchor"],
                "section_path": section["section_path"],
                "offset": section["start_offset"],
                "end_offset": section["end_offset"],
            }
            for section in section_rows
        ]
        return response

    def get_section(self, document_id: str, section: str) -> dict[str, Any] | None:
        document_id = _validate_text(document_id, "document_id", MAX_ID_CHARS).strip()
        if not document_id:
            raise ValueError("document_id must not be empty")
        section = _validate_text(section, "section", MAX_SECTION_CHARS).strip()
        if not section:
            raise ValueError("section must not be empty")
        row = self._conn.execute(
            """
            SELECT d.id, d.source, d.title, d.url, d.text, d.version,
                   d.repository, d.path, d.revision, d.fetched_at,
                   d.metadata_json, d.content_hash,
                   s.ordinal, s.heading, s.level, s.anchor, s.section_path,
                   s.start_offset, s.end_offset, s.content
            FROM documents d JOIN sections s ON s.document_id=d.id
            WHERE d.id=? ORDER BY s.ordinal
            """,
            (document_id,),
        ).fetchall()
        if not row:
            return None
        wanted = _normalise(section.lstrip("#").strip())
        selected: sqlite3.Row | None = None
        if wanted.isdigit():
            ordinal = int(wanted)
            selected = next((candidate for candidate in row if candidate["ordinal"] == ordinal), None)
        if selected is None:
            for candidate in row:
                names = (
                    candidate["heading"],
                    candidate["anchor"],
                    candidate["section_path"],
                )
                if any(_normalise(str(name)) == wanted for name in names):
                    selected = candidate
                    break
        # A document with no explicit heading has a root chunk.  Its title is a
        # useful alias for clients that only know the document title.
        if selected is None and len(row) == 1 and not row[0]["heading"]:
            if wanted in {"root", _normalise(str(row[0]["title"]))}:
                selected = row[0]
        if selected is None:
            return None
        full_content = str(selected["content"])
        section_limit = min(MAX_DOCUMENT_LIMIT, len(full_content))
        section_text = full_content[:section_limit]
        truncated = section_limit < len(full_content)
        return {
            "id": selected["id"],
            "document_id": selected["id"],
            "source": selected["source"],
            "title": selected["title"],
            "url": selected["url"],
            "version": selected["version"],
            "repository": selected["repository"],
            "path": selected["path"],
            "revision": selected["revision"],
            "fetched_at": selected["fetched_at"],
            "metadata": _json_object(selected["metadata_json"]),
            "content_hash": selected["content_hash"],
            "section": selected["heading"] or selected["section_path"],
            "heading": selected["heading"],
            "level": selected["level"],
            "anchor": selected["anchor"],
            "section_path": selected["section_path"],
            "offset": selected["start_offset"],
            "end_offset": selected["end_offset"],
            "text": section_text,
            "total_length": len(full_content),
            "truncated": truncated,
            "next_offset": selected["start_offset"] + section_limit if truncated else None,
        }

    def list_sources(self) -> dict[str, Any]:
        """Return document counts and version/repository provenance."""

        rows = self._conn.execute(
            """
            SELECT source, repository, version, COUNT(*) AS count, MIN(fetched_at) AS oldest, MAX(fetched_at) AS newest
            FROM documents
            GROUP BY source, repository, version
            ORDER BY source, repository, version
            """
        ).fetchall()
        by_source: dict[str, int] = {"official": 0, "github": 0, "docker": 0}
        details: dict[str, dict[str, Any]] = {
            "official": {"count": 0, "repositories": [], "versions": []},
            "github": {"count": 0, "repositories": [], "versions": []},
        }
        for row in rows:
            source = str(row["source"])
            count = int(row["count"])
            by_source[source] = by_source.get(source, 0) + count
            info = details.setdefault(source, {"count": 0, "repositories": [], "versions": []})
            info["count"] += count
            if row["repository"] and row["repository"] not in info["repositories"]:
                info["repositories"].append(row["repository"])
            if row["version"] and row["version"] not in info["versions"]:
                info["versions"].append(row["version"])
            if row["oldest"] and (not info.get("oldest_fetched_at") or row["oldest"] < info["oldest_fetched_at"]):
                info["oldest_fetched_at"] = row["oldest"]
            if row["newest"] and (not info.get("newest_fetched_at") or row["newest"] > info["newest_fetched_at"]):
                info["newest_fetched_at"] = row["newest"]
        total = sum(by_source.values())
        return {
            "total": total,
            "total_documents": total,
            "official": by_source.get("official", 0),
            "github": by_source.get("github", 0),
            "docker": by_source.get("docker", 0),
            "by_source": by_source,
            "sources": details,
        }

    def close(self) -> None:
        """Close the SQLite connection; repeated calls are harmless."""

        connection = getattr(self, "_conn", None)
        if connection is not None:
            self._conn = None  # type: ignore[assignment]
            connection.close()
