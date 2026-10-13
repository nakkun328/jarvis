"""Custom personas persisted in one JSON file (docs/discussion.md). No database table.

The file holds ``{"version": 1, "personas": [...]}``. Writes are atomic (temp file in the same
directory, fsync, ``os.replace``), the file mode is 0600, the directory must already exist (no
parent is ever created), a symlink is refused, and the previous good file is kept as ``<name>.bak``
on every write. A file that cannot be read back as valid is *corrupt*: it is reported with a fixed
code and never overwritten. Errors carry fixed codes only, never persona text.

The model allowlist is checked when a persona is saved (the caller passes it); a stored persona
whose model later leaves the allowlist stays in the file and is re-checked when it is used.
"""

import contextlib
import json
import logging
import os
import threading
from collections.abc import Collection, Sequence
from pathlib import Path

from backend.discussion.errors import DiscussionError, ErrorCode
from backend.discussion.format import RESERVED_SPEAKERS
from backend.discussion.personas import PRESET_NAMES, Persona, persona_from_dict

_LOG = logging.getLogger(__name__)

STORE_VERSION = 1
MAX_STORE_BYTES = 64 * 1024
MAX_CUSTOM_PERSONAS = 20
_RESERVED = frozenset(name.casefold() for name in (*RESERVED_SPEAKERS, *PRESET_NAMES))


def persona_editing_available(path: Path | None) -> bool:
    """Whether the persona file can be written: a path whose directory already exists, and which
    is not a symlink."""
    if path is None:
        return False
    try:
        return path.parent.is_dir() and not path.is_symlink()
    except OSError:
        return False


class PersonaStore:
    def __init__(self, path: Path | None, *, max_personas: int = MAX_CUSTOM_PERSONAS) -> None:
        self._path = path
        self._max = max_personas
        self._lock = threading.Lock()
        self._counter = 0

    @property
    def max_personas(self) -> int:
        return self._max

    def state(self) -> str:
        """``ok``, ``unavailable`` (no usable file location) or ``corrupt``. Never raises."""
        try:
            self.list()
        except DiscussionError as error:
            return "corrupt" if error.code is ErrorCode.STORE_CORRUPT else "unavailable"
        return "ok"

    # ----- reading -----

    def list(self) -> tuple[Persona, ...]:
        with self._lock:
            return self._read_for_write()[0]

    def get(self, name: str) -> Persona | None:
        key = name.casefold() if isinstance(name, str) else ""
        return next((p for p in self.list() if p.name.casefold() == key), None)

    def _need_path(self) -> Path:
        if not persona_editing_available(self._path):
            raise DiscussionError(ErrorCode.STORE_UNAVAILABLE)
        assert self._path is not None
        return self._path

    def _read_raw(self) -> bytes | None:
        path = self._need_path()
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        except OSError:
            raise DiscussionError(ErrorCode.STORE_UNAVAILABLE) from None
        try:
            with os.fdopen(fd, "rb") as handle:
                data = handle.read(MAX_STORE_BYTES + 1)
        except OSError:
            raise DiscussionError(ErrorCode.STORE_UNAVAILABLE) from None
        if len(data) > MAX_STORE_BYTES:
            raise DiscussionError(ErrorCode.STORE_CORRUPT)
        return data

    def _read_for_write(self) -> tuple[tuple[Persona, ...], bytes | None]:
        raw = self._read_raw()
        return (self._decode(raw) if raw is not None else ()), raw

    def _decode(self, data: bytes) -> tuple[Persona, ...]:
        corrupt = DiscussionError(ErrorCode.STORE_CORRUPT)
        try:
            decoded = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise corrupt from None
        if (
            not isinstance(decoded, dict)
            or set(decoded) != {"version", "personas"}
            or isinstance(decoded["version"], bool)
            or decoded["version"] != STORE_VERSION
            or not isinstance(decoded["personas"], list)
            or len(decoded["personas"]) > self._max
        ):
            raise corrupt
        try:
            personas = tuple(
                persona_from_dict(item, index) for index, item in enumerate(decoded["personas"])
            )
        except DiscussionError:
            raise corrupt from None
        keys = [persona.name.casefold() for persona in personas]
        if len(set(keys)) != len(keys) or any(key in _RESERVED for key in keys):
            raise corrupt
        return personas

    # ----- writing -----

    def create(self, persona: Persona, allowed_models: Collection[str]) -> Persona:
        with self._lock:
            personas, raw = self._read_for_write()
            self._check_saveable(persona, allowed_models)
            if len(personas) >= self._max:
                raise DiscussionError(ErrorCode.LIMIT)
            self._check_name_free(persona, personas, skip=None)
            self._write((*personas, persona), raw)
        return persona

    def replace(self, name: str, persona: Persona, allowed_models: Collection[str]) -> Persona:
        with self._lock:
            personas, raw = self._read_for_write()
            index = self._index(personas, name)
            self._check_saveable(persona, allowed_models)
            self._check_name_free(persona, personas, skip=index)
            updated = list(personas)
            updated[index] = persona
            self._write(tuple(updated), raw)
        return persona

    def delete(self, name: str) -> None:
        with self._lock:
            personas, raw = self._read_for_write()
            index = self._index(personas, name)
            self._write(tuple(p for i, p in enumerate(personas) if i != index), raw)

    @staticmethod
    def _index(personas: Sequence[Persona], name: str) -> int:
        key = name.casefold() if isinstance(name, str) else ""
        for index, persona in enumerate(personas):
            if persona.name.casefold() == key:
                return index
        raise DiscussionError(ErrorCode.NOT_FOUND)

    @staticmethod
    def _check_saveable(persona: Persona, allowed_models: Collection[str]) -> None:
        if persona.name.casefold() in _RESERVED:
            raise DiscussionError(ErrorCode.NAME_RESERVED)
        persona.require_allowed(allowed_models)

    @staticmethod
    def _check_name_free(persona: Persona, personas: Sequence[Persona], skip: int | None) -> None:
        key = persona.name.casefold()
        for index, other in enumerate(personas):
            if index != skip and other.name.casefold() == key:
                raise DiscussionError(ErrorCode.DUPLICATE_NAME)

    def _write(self, personas: tuple[Persona, ...], previous: bytes | None) -> None:
        document = {"version": STORE_VERSION, "personas": [p.as_dict() for p in personas]}
        data = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(data) > MAX_STORE_BYTES:
            raise DiscussionError(ErrorCode.STORE_TOO_LARGE)
        path = self._need_path()
        backup = path.with_name(path.name + ".bak")
        try:
            if backup.is_symlink():
                raise DiscussionError(ErrorCode.STORE_UNAVAILABLE)
            if previous is not None:
                self._atomic_write(backup, previous)
            self._atomic_write(path, data)
        except OSError as error:
            _LOG.warning(
                "discussion.persona_store_failed", extra={"error_type": type(error).__name__}
            )
            raise DiscussionError(ErrorCode.STORE_UNAVAILABLE) from None

    def _atomic_write(self, target: Path, data: bytes) -> None:
        self._counter += 1
        temporary = target.with_name(f".{target.name}.{os.getpid()}.{self._counter}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except BaseException:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise
        with contextlib.suppress(OSError):
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
