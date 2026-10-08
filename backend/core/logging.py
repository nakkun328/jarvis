"""Structured JSON-lines logging with redaction and a reusable correlation context.

Every record becomes one JSON object per line with fixed fields (``timestamp``, ``level``,
``logger``, ``event``), the active log context (for example ``request_id``), bounded extra
fields, and, for exceptions, only the error type and sanitized traceback frames.

Redaction happens while formatting, so records shared with other handlers are not mutated and
anything reaching a JARVIS handler passes through it. If redaction of a field fails, that field
is dropped instead of being emitted raw.
"""

import json
import logging
import math
import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType, TracebackType
from uuid import UUID

REDACTED = "[REDACTED]"
DROPPED_EVENT = "[log message dropped: redaction failed]"
REQUEST_ID_KEY = "request_id"

_MAX_EVENT_CHARS = 4000
_MAX_STRING_CHARS = 2000
_MAX_INPUT_CHARS = 32768  # bound regex work on hostile input before redaction
_MAX_EXTRA_FIELDS = 24
_MAX_KEY_CHARS = 64
_MAX_ITEMS = 20
_MAX_DEPTH = 4
_MAX_FRAMES = 30
_MAX_CHAIN = 5

_FIXED_FIELDS = ("timestamp", "level", "logger", "event")
_RECORD_ATTRIBUTES = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "color_message",  # uvicorn's ANSI duplicate of the message
}
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_KEY_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")

_Scalar = str | int | float | bool | None

# --- Redaction -------------------------------------------------------------------------------

_SENSITIVE_SEGMENTS = frozenset(
    {
        "authorization",
        "password",
        "passwd",
        "secret",
        "token",
        "cookie",
        "credential",
        "credentials",
        "apikey",
    }
)
_SENSITIVE_PAIRS = frozenset({("api", "key"), ("private", "key"), ("access", "key")})
_NAME = r"[A-Za-z0-9_.-]{0,40}"
_NAME_WORDS = r"(?:api[_-]?key|secret|passw(?:or)?d|token|credential|private[_-]?key)"
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN[ A-Z]{0,30}PRIVATE KEY-----.*?(?:-----END[ A-Z]{0,30}PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
        "[REDACTED PRIVATE KEY]",
    ),
    (re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"AIza[A-Za-z0-9_-]{20,}"), REDACTED),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), REDACTED),
    (re.compile(r"\b(Bearer)\s+[A-Za-z0-9._~+/=-]{4,}", re.IGNORECASE), r"\1 " + REDACTED),
    (
        re.compile(
            r"(\bauthorization[\"']?\s*[:=]\s*)"
            r"(?:\"[^\"]*\"|'[^']*'|[A-Za-z]+\s+[^\s,;\"']+|[^\s,;\"']+)",
            re.IGNORECASE,
        ),
        r"\1" + REDACTED,
    ),
    (
        re.compile(
            r"(\b" + _NAME + _NAME_WORDS + _NAME + r"[\"']?\s*[:=]\s*)"
            r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&\"'}\]]+)",
            re.IGNORECASE,
        ),
        r"\1" + REDACTED,
    ),
)


def is_sensitive_key(key: object) -> bool:
    """Return True for names such as ``Authorization``, ``api_key`` or ``X-Auth-Token``."""
    segments = re.split(r"[^a-z0-9]+", str(key).lower())
    if any(segment in _SENSITIVE_SEGMENTS for segment in segments):
        return True
    return any(pair in _SENSITIVE_PAIRS for pair in zip(segments, segments[1:], strict=False))


def redact_text(text: str) -> str:
    """Replace known credential shapes in free text. Raises if redaction cannot complete."""
    text = text[:_MAX_INPUT_CHARS]
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"...[+{len(text) - limit} chars]"


def redact_value(value: object, *, _depth: int = 0) -> object:
    """Return a JSON-safe, bounded, redacted copy of ``value``. Raises on redaction failure.

    Unknown object types become ``<TypeName>``; their ``str()`` is never used because it can
    echo payloads.
    """
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _truncate(redact_text(value), _MAX_STRING_CHARS)
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<bytes len={len(value)}>"
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return redact_value(str(value.name), _depth=_depth + 1)
    if isinstance(value, BaseException):
        return f"<{type(value).__name__}>"
    if _depth >= _MAX_DEPTH:
        return "<max depth>"
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_ITEMS:
                result["..."] = f"+{len(value) - _MAX_ITEMS} more"
                break
            name = _truncate(redact_text(str(key)), _MAX_KEY_CHARS)
            result[name] = (
                REDACTED if is_sensitive_key(key) else redact_value(item, _depth=_depth + 1)
            )
        return result
    if isinstance(value, list | tuple | set | frozenset):
        items = list(value)
        shown = [redact_value(item, _depth=_depth + 1) for item in items[:_MAX_ITEMS]]
        if len(items) > _MAX_ITEMS:
            shown.append(f"...+{len(items) - _MAX_ITEMS} more")
        return shown
    return f"<{type(value).__name__}>"


# --- Log context ------------------------------------------------------------------------------

_CONTEXT: ContextVar[Mapping[str, _Scalar]] = ContextVar(
    "jarvis_log_context", default=MappingProxyType({})
)


def current_log_context() -> dict[str, _Scalar]:
    """Return a copy of the fields attached to every record in this execution context."""
    return dict(_CONTEXT.get())


@contextmanager
def log_context(**fields: _Scalar) -> Iterator[None]:
    """Attach scalar fields (for example ``request_id``) to every record logged inside.

    Contexts nest: inner values override outer ones and the previous context is restored on
    exit. The context follows ``contextvars``, so it reaches ``await``ed code, threadpool
    calls, and tasks spawned inside the block, but never other requests. Later features such as
    Research or Task runs can reuse this (``log_context(task_id=...)``) without changes here.
    Names must match ``[A-Za-z_][A-Za-z0-9_.-]*`` and values must be str, int, float, bool or
    None; anything else is ignored, and values are redacted when a record is formatted.
    """
    valid = {
        key: value
        for key, value in fields.items()
        if _KEY_NAME.fullmatch(key)
        and key not in _FIXED_FIELDS
        and (value is None or isinstance(value, str | int | float | bool))
    }
    token = _CONTEXT.set(MappingProxyType({**_CONTEXT.get(), **valid}))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


# --- Formatting -------------------------------------------------------------------------------


def _frame_location(filename: str) -> str:
    """Return a path that cannot reveal home directories or data locations."""
    path = Path(filename)
    try:
        return path.resolve().relative_to(_PROJECT_ROOT).as_posix()
    except (OSError, ValueError):
        pass
    parts = path.parts
    if "site-packages" in parts:
        return "/".join(parts[parts.index("site-packages") + 1 :])
    return "/".join(parts[-2:])


def _frames(tb: TracebackType | None) -> list[str]:
    frames: list[str] = []
    while tb is not None:
        code = tb.tb_frame.f_code
        location = f"{_frame_location(code.co_filename)}:{tb.tb_lineno} in {code.co_name}"
        frames.append(redact_text(location))
        tb = tb.tb_next
    return frames[-_MAX_FRAMES:]


def _error_fields(
    exc_info: tuple[type[BaseException], BaseException, TracebackType | None],
) -> dict[str, object]:
    """Describe an exception without ``str(exc)``: types and code locations only.

    ``traceback`` holds the frames of the logged exception; when it wraps another exception,
    ``error_chain`` lists the cause types and ``cause_traceback`` the frames of the root cause.
    """
    _, exc, tb = exc_info
    fields: dict[str, object] = {"error_type": type(exc).__name__}
    chain: list[BaseException] = []
    seen = {id(exc)}
    link = exc.__cause__ or exc.__context__
    while link is not None and id(link) not in seen and len(chain) < _MAX_CHAIN:
        seen.add(id(link))
        chain.append(link)
        link = link.__cause__ or link.__context__
    if chain:
        fields["error_chain"] = [type(item).__name__ for item in chain]
    if frames := _frames(tb):
        fields["traceback"] = frames
    if chain and (cause_frames := _frames(chain[-1].__traceback__)):
        fields["cause_traceback"] = cause_frames
    return fields


class JsonLogFormatter(logging.Formatter):
    """Format records as single-line JSON objects. Never raises and never emits raw secrets."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            payload = self._payload(record)
        except Exception:
            payload = {**self._base(record), "event": "log_format_failed"}
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _base(record: logging.LogRecord) -> dict[str, object]:
        created = datetime.fromtimestamp(record.created, UTC)
        return {
            "timestamp": created.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
        }

    def _payload(self, record: logging.LogRecord) -> dict[str, object]:
        payload = self._base(record)
        try:
            raw = record.getMessage()
        except Exception:
            raw = str(record.msg)
        try:
            payload["event"] = _truncate(redact_text(raw), _MAX_EVENT_CHARS)
        except Exception:
            payload["event"] = DROPPED_EVENT

        def add(name: str, value: object) -> None:
            key = name if name not in payload else f"extra_{name}"
            if key in payload:
                return
            try:
                payload[key] = REDACTED if is_sensitive_key(name) else redact_value(value)
            except Exception:
                return  # fail safe: drop the field instead of emitting it raw

        for key, value in _CONTEXT.get().items():
            add(key, value)
        extras = [(k, v) for k, v in record.__dict__.items() if k not in _RECORD_ATTRIBUTES]
        for key, value in extras[:_MAX_EXTRA_FIELDS]:
            add(_truncate(str(key), _MAX_KEY_CHARS), value)
        if len(extras) > _MAX_EXTRA_FIELDS:
            payload["fields_dropped"] = len(extras) - _MAX_EXTRA_FIELDS
        if record.exc_info and record.exc_info[1] is not None:
            try:
                payload.update(_error_fields(record.exc_info))  # type: ignore[arg-type]
            except Exception:
                payload["error_type"] = "unknown"
        # exc_text and stack_info are intentionally not emitted: they embed exception messages
        # and source lines that can echo upstream payloads.
        return payload


def _adopt_uvicorn_loggers() -> None:
    """Route uvicorn output through the root JSON handler; the access log is superseded."""
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True  # request logging middleware records method/path/status instead


def configure_logging(level: str) -> None:
    """Install one JSON-lines stderr handler on the root logger. Safe to call repeatedly."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLogFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    _adopt_uvicorn_loggers()
