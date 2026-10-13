"""Persona definitions for the group discussion (docs/discussion.md).

A persona is the owner's own instruction text for one participant. Validation is strict and
fail-closed; an error carries a fixed code and never the offending text. The model is an exact
entry of the server allowlist (``provider:model``, the same rule as ``JARVIS_MODEL_CHOICES``),
never a free-form value, and a persona holds no credential of any kind.
"""

import json
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.core.config import ConfigError, parse_model_choices
from backend.discussion.errors import DiscussionError, ErrorCode

NAME_MAX = 20
ROLE_MAX = 100
STANCE_MAX = 500
CHECKLIST_MAX_ITEMS = 5
CHECKLIST_ITEM_MAX = 100
PERSONAS_MIN = 2
PERSONAS_MAX = 4
MAX_FILE_BYTES = 64 * 1024
#: The fixed palette (names, not CSS: the screen maps them to colors).
PALETTE = ("blue", "green", "orange", "purple", "red", "teal")

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # keeps tab and newline only
_NAME_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f【】\[\]<>]")
_FIELDS = frozenset({"name", "role", "stance", "checklist", "model", "color"})


@dataclass(frozen=True)
class Persona:
    name: str
    role: str
    stance: str
    checklist: tuple[str, ...]
    model: str
    color: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _NAME_FORBIDDEN.search(self.name):
            raise DiscussionError(ErrorCode.NAME_INVALID)
        if not 1 <= len(self.name) <= NAME_MAX or self.name != self.name.strip():
            raise DiscussionError(ErrorCode.NAME_INVALID)
        if (
            not isinstance(self.role, str)
            or "\n" in self.role
            or _CONTROL.search(self.role)
            or not 1 <= len(self.role.strip()) <= ROLE_MAX
        ):
            raise DiscussionError(ErrorCode.ROLE_INVALID)
        if (
            not isinstance(self.stance, str)
            or _CONTROL.search(self.stance)
            or len(self.stance) > STANCE_MAX
        ):
            raise DiscussionError(ErrorCode.STANCE_INVALID)
        items = self.checklist
        if (
            not isinstance(items, tuple)
            or len(items) > CHECKLIST_MAX_ITEMS
            or not all(
                isinstance(item, str)
                and 1 <= len(item.strip()) <= CHECKLIST_ITEM_MAX
                and "\n" not in item
                and not _CONTROL.search(item)
                for item in items
            )
        ):
            raise DiscussionError(ErrorCode.CHECKLIST_INVALID)
        if not isinstance(self.model, str) or not _is_allowlist_entry(self.model):
            raise DiscussionError(ErrorCode.MODEL_INVALID)
        if self.color not in PALETTE:
            raise DiscussionError(ErrorCode.COLOR_INVALID)

    def require_allowed(self, allowed: Collection[str]) -> None:
        """Refuse a model that is not exactly one of the server's allowlist entries."""
        if self.model not in allowed:
            raise DiscussionError(ErrorCode.MODEL_NOT_ALLOWED)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "stance": self.stance,
            "checklist": list(self.checklist),
            "model": self.model,
            "color": self.color,
        }


def _is_allowlist_entry(value: str) -> bool:
    if "," in value:
        return False
    try:
        return parse_model_choices(value) == (value,)
    except ConfigError:
        return False


# Built-in presets (docs/group-discussion-plan.md section 1). Models are assigned from the
# server's allowlist when the presets are built, so the text is fixed and the models are not.
_PRESETS: tuple[tuple[str, str, str, tuple[str, ...], str], ...] = (
    (
        "設計者",
        "案を出す",
        "全体の筋を通し、具体的な案と代案を示す。落ち着いた口調で話す。",
        ("全体の筋", "代案"),
        "blue",
    ),
    (
        "批判役",
        "前提を疑い、穴を探す",
        "遠慮せずに、ただし礼儀正しく疑う。反対するときは根拠を必ず添える。",
        ("前提の誤り", "数字の根拠", "見落とし", "反対の根拠を必ず1つ挙げる"),
        "red",
    ),
    (
        "現実担当",
        "実行可能性を見る",
        "現場の感覚で、実際にできるかどうかを確かめる。簡潔に話す。",
        ("費用", "時間", "手間", "安全"),
        "green",
    ),
)
PRESET_NAMES = tuple(entry[0] for entry in _PRESETS)


def preset_personas(models: Sequence[str]) -> tuple[Persona, ...]:
    """The three built-in personas. ``models`` are allowlist entries, used in turn."""
    if not models:
        raise DiscussionError(ErrorCode.MODEL_INVALID)
    return tuple(
        Persona(name, role, stance, checklist, models[index % len(models)], color)
        for index, (name, role, stance, checklist, color) in enumerate(_PRESETS)
    )


def _persona_from(raw: object, index: int, allowed: Collection[str]) -> Persona:
    if not isinstance(raw, Mapping) or set(raw) - _FIELDS:
        raise DiscussionError(
            ErrorCode.FIELD_UNKNOWN if isinstance(raw, Mapping) else ErrorCode.PERSONA_INVALID
        )
    checklist = raw.get("checklist", [])
    if not isinstance(checklist, list):
        raise DiscussionError(ErrorCode.CHECKLIST_INVALID)
    persona = Persona(
        name=raw.get("name"),  # type: ignore[arg-type]  # validated in __post_init__
        role=raw.get("role"),  # type: ignore[arg-type]
        stance=raw.get("stance", ""),
        checklist=tuple(checklist),
        model=raw.get("model"),  # type: ignore[arg-type]
        color=raw.get("color", PALETTE[index % len(PALETTE)]),
    )
    persona.require_allowed(allowed)
    return persona


def parse_personas(raw: object, allowed_models: Collection[str]) -> tuple[Persona, ...]:
    """Personas from decoded JSON: a list, or an object with a ``personas`` list.

    2..4 personas, distinct names, every model an exact allowlist entry. Raises
    ``DiscussionError`` with a fixed code.
    """
    if isinstance(raw, Mapping) and set(raw) == {"personas"}:
        raw = raw["personas"]
    if not isinstance(raw, list):
        raise DiscussionError(ErrorCode.FILE_INVALID)
    if not PERSONAS_MIN <= len(raw) <= PERSONAS_MAX:
        raise DiscussionError(ErrorCode.COUNT_INVALID)
    personas = tuple(_persona_from(item, index, allowed_models) for index, item in enumerate(raw))
    check_distinct(personas)
    return personas


def check_distinct(personas: Sequence[Persona]) -> None:
    names = [persona.name.casefold() for persona in personas]
    if len(set(names)) != len(names):
        raise DiscussionError(ErrorCode.DUPLICATE_NAME)


def load_personas(path: Path, allowed_models: Collection[str]) -> tuple[Persona, ...]:
    """Personas from a JSON file (not wired to any setting yet)."""
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise DiscussionError(ErrorCode.FILE_UNREADABLE) from None
    if len(data) > MAX_FILE_BYTES:
        raise DiscussionError(ErrorCode.FILE_INVALID)
    try:
        decoded = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise DiscussionError(ErrorCode.FILE_INVALID) from None
    return parse_personas(decoded, allowed_models)
