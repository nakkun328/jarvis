"""Validated personality settings and their editable TOML file format.

The file may only choose among predefined levels. It never supplies prompt text.
"""

import os
import stat
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 4096
SCHEMA_VERSION = 1


class PersonalityError(ValueError):
    """The personality settings are invalid or cannot be loaded safely."""


class Formality(StrEnum):
    CASUAL = "casual"
    POLITE = "polite"
    FORMAL = "formal"


class Humor(StrEnum):
    OFF = "off"
    LIGHT = "light"
    MODERATE = "moderate"


class Sarcasm(StrEnum):
    OFF = "off"
    LIGHT = "light"


class Initiative(StrEnum):
    NONE = "none"
    ONE = "one"
    FEW = "few"


class Verbosity(StrEnum):
    BRIEF = "brief"
    CONCISE = "concise"
    DETAILED = "detailed"


class Caution(StrEnum):
    STANDARD = "standard"
    HIGH = "high"


_FIELD_TYPES: dict[str, type[StrEnum]] = {
    "formality": Formality,
    "humor": Humor,
    "sarcasm": Sarcasm,
    "initiative": Initiative,
    "verbosity": Verbosity,
    "caution": Caution,
}


@dataclass(frozen=True)
class PersonalityProfile:
    """One level for each managed item. Defaults are the documented initial values."""

    formality: Formality = Formality.POLITE
    humor: Humor = Humor.LIGHT
    sarcasm: Sarcasm = Sarcasm.OFF
    initiative: Initiative = Initiative.ONE
    verbosity: Verbosity = Verbosity.CONCISE
    caution: Caution = Caution.STANDARD

    def __post_init__(self) -> None:
        for name, level_type in _FIELD_TYPES.items():
            if not isinstance(getattr(self, name), level_type):
                raise PersonalityError(f"{name} must be a {level_type.__name__} level")

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "PersonalityProfile":
        """Validate raw settings strictly: no unknown keys, exact lowercase level names."""
        unknown = sorted(set(values) - set(_FIELD_TYPES))
        if unknown:
            shown = ", ".join(repr(key[:40]) for key in unknown[:5])
            raise PersonalityError(
                f"unknown personality setting(s): {shown}; "
                f"allowed: {', '.join(_FIELD_TYPES)}"
            )
        levels: dict[str, StrEnum] = {}
        for name, raw in values.items():
            level_type = _FIELD_TYPES[name]
            allowed = ", ".join(level.value for level in level_type)
            if not isinstance(raw, str):
                raise PersonalityError(f"{name} must be a string, one of: {allowed}")
            try:
                levels[name] = level_type(raw)
            except ValueError:
                raise PersonalityError(f"{name} must be one of: {allowed}") from None
        return cls(**levels)

    def describe(self) -> str:
        """Level names only, safe to log."""
        return " ".join(f"{name}={getattr(self, name).value}" for name in _FIELD_TYPES)


DEFAULT_PROFILE = PersonalityProfile()


def parse_personality(raw: bytes) -> PersonalityProfile:
    if len(raw) > MAX_FILE_BYTES:
        raise PersonalityError(f"personality file exceeds {MAX_FILE_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PersonalityError("personality file must be UTF-8 text") from None
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise PersonalityError(f"personality file is not valid TOML: {error}") from None
    version = document.pop("version", SCHEMA_VERSION)
    if type(version) is not int or version != SCHEMA_VERSION:
        raise PersonalityError(f"personality file version must be {SCHEMA_VERSION}")
    table = document.pop("personality", {})
    if document:
        shown = ", ".join(repr(key[:40]) for key in sorted(document)[:5])
        raise PersonalityError(
            f"unknown top-level key(s): {shown}; allowed: version, [personality]"
        )
    if not isinstance(table, dict):
        raise PersonalityError("[personality] must be a table")
    return PersonalityProfile.from_mapping(table)


def load_personality(path: Path | None) -> PersonalityProfile:
    """Return defaults for no path; otherwise read and validate the configured file.

    A configured path must be a regular file. A symlink in the final component is
    rejected; symlinked parent directories are resolved by the operating system.
    """
    if path is None:
        return DEFAULT_PROFILE
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise PersonalityError("JARVIS_PERSONALITY_PATH must not be a symlink")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise PersonalityError("JARVIS_PERSONALITY_PATH does not exist") from None
    except OSError:
        raise PersonalityError("JARVIS_PERSONALITY_PATH cannot be opened") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PersonalityError("JARVIS_PERSONALITY_PATH must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise PersonalityError("JARVIS_PERSONALITY_PATH cannot be read") from None
    finally:
        os.close(descriptor)
    return parse_personality(raw)
