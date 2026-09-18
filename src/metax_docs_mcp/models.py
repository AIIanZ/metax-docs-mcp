"""Data models used by the MetaX documentation index.

The ingestion implementations deliberately only need to know about this small
model.  Keeping it as a plain dataclass also makes it convenient for callers
to construct records from either the official documentation site or GitHub.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Document:
    """One versioned source document to be stored in the local index.

    ``id`` is assigned by the source adapter and is expected to be stable for
    the same canonical source object/version.  The store validates the
    externally supplied values before writing them to SQLite; the dataclass
    intentionally remains lightweight so source adapters can build records in
    stages.
    """

    id: str
    source: str
    title: str
    url: str
    text: str
    version: str = ""
    repository: str = ""
    path: str = ""
    revision: str = ""
    fetched_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

