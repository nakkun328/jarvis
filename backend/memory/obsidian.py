"""Small, editable Markdown vault adapter for long-term memory notes."""

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias
from uuid import UUID

MetadataValue: TypeAlias = str | int | float | bool | None | list[str]
_KEY = re.compile(r"[a-z][a-z0-9_]*\Z")


class VaultError(RuntimeError):
    """A vault note cannot be used safely."""


class VaultConflictError(VaultError):
    """The note changed since it was last read."""


class VaultFormatError(VaultError):
    """The note does not use the adapter's simple frontmatter format."""


@dataclass(frozen=True)
class VaultDocument:
    memory_id: UUID
    body: str
    metadata: dict[str, MetadataValue]
    revision: str
    path: Path


class ObsidianVault:
    """Store one Markdown note per UUID, with optimistic update checks.

    Only JSON-compatible, single-line YAML frontmatter fields are supported.
    Obsidian users may edit both the body and fields within that format.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def create(
        self, memory_id: UUID, body: str, metadata: Mapping[str, MetadataValue]
    ) -> VaultDocument:
        path = self._path(memory_id)
        content = _encode(memory_id, body, metadata)
        self._prepare_root()
        temporary = self._write_temporary(content)
        try:
            # Linking a completed file creates the destination exclusively.
            os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return VaultDocument(memory_id, body, dict(metadata), _revision(content), path)

    def read(self, memory_id: UUID) -> VaultDocument | None:
        path = self._path(memory_id)
        self._check_root()
        try:
            content = self._read_bytes(path)
        except FileNotFoundError:
            return None
        body, metadata = _decode(memory_id, content)
        return VaultDocument(memory_id, body, metadata, _revision(content), path)

    def update(
        self,
        memory_id: UUID,
        body: str,
        metadata: Mapping[str, MetadataValue],
        *,
        expected_revision: str,
    ) -> VaultDocument:
        path = self._path(memory_id)
        content = _encode(memory_id, body, metadata)
        self._check_root()
        if _revision(self._read_bytes(path)) != expected_revision:
            raise VaultConflictError(f"Vault note changed: {memory_id}")
        temporary = self._write_temporary(content)
        try:
            # Recheck immediately before replacement. External editors do not
            # participate in a lock, so this cannot close every race.
            if _revision(self._read_bytes(path)) != expected_revision:
                raise VaultConflictError(f"Vault note changed: {memory_id}")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return VaultDocument(memory_id, body, dict(metadata), _revision(content), path)

    def _path(self, memory_id: UUID) -> Path:
        if not isinstance(memory_id, UUID):
            raise TypeError("memory_id must be a UUID")
        return self.root / f"{memory_id}.md"

    def _prepare_root(self) -> None:
        if self.root.is_symlink():
            raise VaultError("Vault root must not be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_root()

    def _check_root(self) -> None:
        try:
            mode = self.root.lstat().st_mode
        except FileNotFoundError as exc:
            raise VaultError("Vault root does not exist") from exc
        if not stat.S_ISDIR(mode):
            raise VaultError("Vault root must be a directory, not a symlink")

    @staticmethod
    def _read_bytes(path: Path) -> bytes:
        if path.is_symlink():
            raise VaultError(f"Vault note must not be a symlink: {path.name}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise VaultError(f"Vault note must be a regular file: {path.name}")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                return source.read()
        finally:
            os.close(descriptor)

    def _write_temporary(self, content: bytes) -> Path:
        descriptor, name = tempfile.mkstemp(prefix=".jarvis-", suffix=".tmp", dir=self.root)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(content)
                destination.flush()
                os.fsync(destination.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return temporary


def _revision(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _valid_metadata(metadata: Mapping[str, MetadataValue]) -> None:
    if not isinstance(metadata, Mapping):
        raise TypeError("metadata must be a mapping")
    for key, value in metadata.items():
        if not isinstance(key, str) or not _KEY.fullmatch(key) or key == "id":
            raise ValueError(f"Invalid or reserved metadata key: {key!r}")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"Non-finite metadata value: {key}")
        if isinstance(value, list):
            if not all(isinstance(item, str) for item in value):
                raise ValueError(f"Metadata list must contain strings: {key}")
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"Unsupported metadata value: {key}")


def _encode(memory_id: UUID, body: str, metadata: Mapping[str, MetadataValue]) -> bytes:
    if not isinstance(body, str):
        raise TypeError("body must be a string")
    _valid_metadata(metadata)
    fields = {"id": str(memory_id), **metadata}
    lines = ["---"]
    for key, value in fields.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False, allow_nan=False)}")
    lines.extend(("---", body))
    return "\n".join(lines).encode("utf-8")


def _decode(memory_id: UUID, content: bytes) -> tuple[str, dict[str, MetadataValue]]:
    try:
        markdown = content.decode("utf-8")
        frontmatter, body = markdown.removeprefix("---\n").split("\n---\n", 1)
        if not markdown.startswith("---\n"):
            raise ValueError("Missing frontmatter")
        fields = {}
        for line in frontmatter.splitlines():
            key, raw = line.split(": ", 1)
            if key in fields:
                raise ValueError("Duplicate frontmatter key")
            fields[key] = json.loads(raw)
        if fields.pop("id") != str(memory_id):
            raise ValueError("Frontmatter ID differs from filename")
        _valid_metadata(fields)
    except (UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise VaultFormatError(f"Invalid frontmatter in {memory_id}.md") from exc
    return body, fields
