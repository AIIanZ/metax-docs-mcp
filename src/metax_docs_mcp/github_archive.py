"""Read selected files from a bounded GitHub snapshot without extracting it."""

from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import PurePosixPath
from typing import Iterator


MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_MEMBERS = 100_000
MAX_FILE_BYTES = 8 * 1024 * 1024


class _BoundedReader:
    def __init__(self, stream, maximum: int):
        self.stream = stream
        self.maximum = maximum
        self.total = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self.maximum - self.total
        data = self.stream.read(min(size, remaining + 1) if size >= 0 else remaining + 1)
        self.total += len(data)
        if self.total > self.maximum:
            raise ValueError("GitHub archive expanded byte limit exceeded")
        return data


def iter_archive_files(content: bytes, paths: set[str]) -> Iterator[tuple[str, bytes]]:
    """Yield regular requested files; never follow links or write archive paths."""

    found: set[str] = set()
    root: str | None = None
    with gzip.GzipFile(fileobj=io.BytesIO(content)) as expanded:
        with tarfile.open(fileobj=_BoundedReader(expanded, MAX_EXPANDED_BYTES), mode="r|") as archive:
            for index, member in enumerate(archive):
                if index >= MAX_MEMBERS:
                    raise ValueError("GitHub archive member limit exceeded")
                parts = PurePosixPath(member.name).parts
                if member.name.startswith("/") or ".." in parts or not parts:
                    raise ValueError("unsafe GitHub archive path")
                if root is None:
                    root = parts[0]
                if parts[0] != root:
                    raise ValueError("GitHub archive has multiple roots")
                path = "/".join(parts[1:])
                if path not in paths or not member.isfile():
                    continue
                if path in found:
                    raise ValueError("duplicate GitHub archive file")
                if member.size > MAX_FILE_BYTES:
                    raise ValueError(f"GitHub archive file byte limit: {path}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError(f"unreadable GitHub archive file: {path}")
                raw = stream.read(MAX_FILE_BYTES + 1)
                if len(raw) != member.size:
                    raise ValueError(f"incomplete GitHub archive file: {path}")
                found.add(path)
                yield path, raw
    missing = paths - found
    if missing:
        raise ValueError(f"GitHub archive missing selected files: {', '.join(sorted(missing)[:3])}")
