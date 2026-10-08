"""Tool contract: specs, calls, results, errors, and a bounded JSON-Schema subset.

Everything a model produces (tool names, arguments) is untrusted data. The validator here is
deliberately small and strict: unknown schema keywords are rejected, objects reject extra keys
unless the schema opts in, and depth, node count, and string size are bounded.
"""

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1000
MAX_TIMEOUT_SECONDS = 3600.0
MAX_VIOLATIONS = 16
MAX_PATH_LENGTH = 120
INTEGER_BOUND = 2**63

_NAME_RE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*")
_LABEL_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_VERSION_RE = re.compile(r"\d{1,4}\.\d{1,4}\.\d{1,4}")
_CALL_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")

INVALID_NAME_LABEL = "<invalid>"


class PermissionLevel(StrEnum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class ToolStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    DENIED = "denied"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INVALID_ARGUMENTS = "invalid_arguments"


class ToolErrorCode(StrEnum):
    """Fixed error vocabulary. Upstream exception text is never carried in a result."""

    UNKNOWN_TOOL = "unknown_tool"
    TOOL_UNAVAILABLE = "tool_unavailable"
    INVALID_ARGUMENTS = "invalid_arguments"
    PERMISSION_DENIED = "permission_denied"
    CONFIRMATION_REQUIRED = "confirmation_required"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"
    INVALID_OUTPUT = "invalid_output"


_ALLOWED_ERRORS: Mapping[ToolStatus, frozenset[ToolErrorCode]] = MappingProxyType(
    {
        ToolStatus.OK: frozenset(),
        ToolStatus.ERROR: frozenset(
            {
                ToolErrorCode.UNKNOWN_TOOL,
                ToolErrorCode.TOOL_UNAVAILABLE,
                ToolErrorCode.INTERNAL_ERROR,
                ToolErrorCode.INVALID_OUTPUT,
            }
        ),
        ToolStatus.DENIED: frozenset(
            {ToolErrorCode.PERMISSION_DENIED, ToolErrorCode.CONFIRMATION_REQUIRED}
        ),
        ToolStatus.TIMEOUT: frozenset({ToolErrorCode.TIMEOUT}),
        ToolStatus.CANCELLED: frozenset({ToolErrorCode.CANCELLED}),
        ToolStatus.INVALID_ARGUMENTS: frozenset({ToolErrorCode.INVALID_ARGUMENTS}),
    }
)


class SchemaViolationCode(StrEnum):
    TYPE = "type"
    REQUIRED = "required"
    ENUM = "enum"
    LENGTH = "length"
    ITEM_COUNT = "item_count"
    RANGE = "range"
    ADDITIONAL_PROPERTY = "additional_property"
    NOT_FINITE = "not_finite"
    KEY_TYPE = "key_type"
    DEPTH = "depth"
    SIZE = "size"


@dataclass(frozen=True)
class SchemaViolation:
    """Where and why a value was rejected. Never contains the rejected value itself."""

    path: str
    code: SchemaViolationCode


class SchemaDefinitionError(ValueError):
    """A tool schema is outside the supported subset."""


class ToolSpecError(ValueError):
    """A tool specification is invalid."""


@dataclass(frozen=True)
class SchemaLimits:
    max_depth: int = 8
    max_nodes: int = 10_000
    max_properties: int = 256
    max_items: int = 1_000
    max_string_length: int = 65_536
    max_total_chars: int = 1_000_000


DEFAULT_LIMITS = SchemaLimits()

_SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean", "null"})
_ALL_TYPES = _SCALAR_TYPES | {"object", "array"}
_COMMON_KEYWORDS = frozenset({"type", "description", "enum"})
_KEYWORDS_BY_TYPE: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "object": _COMMON_KEYWORDS | {"properties", "required", "additionalProperties"},
        "array": _COMMON_KEYWORDS | {"items", "minItems", "maxItems"},
        "string": _COMMON_KEYWORDS | {"minLength", "maxLength"},
        "integer": _COMMON_KEYWORDS | {"minimum", "maximum"},
        "number": _COMMON_KEYWORDS | {"minimum", "maximum"},
        "boolean": _COMMON_KEYWORDS,
        "null": _COMMON_KEYWORDS,
    }
)
_MAX_ENUM_VALUES = 64


def _is_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _check_count(schema: Mapping[str, Any], key: str, where: str) -> None:
    if key in schema:
        value = schema[key]
        if not _is_plain_int(value) or not 0 <= value <= 1_000_000:
            raise SchemaDefinitionError(f"{where}: {key} must be a non-negative integer")


def _check_schema_node(schema: object, where: str, depth: int, limits: SchemaLimits) -> None:
    if depth > limits.max_depth:
        raise SchemaDefinitionError(f"{where}: schema nested too deeply")
    if not isinstance(schema, Mapping):
        raise SchemaDefinitionError(f"{where}: schema must be an object")
    kind = schema.get("type")
    if not isinstance(kind, str) or kind not in _ALL_TYPES:
        raise SchemaDefinitionError(f"{where}: type must be one of {sorted(_ALL_TYPES)}")
    unknown = set(schema) - _KEYWORDS_BY_TYPE[kind]
    if unknown:
        raise SchemaDefinitionError(f"{where}: unsupported keyword(s) {sorted(map(str, unknown))}")
    if "description" in schema and not isinstance(schema["description"], str):
        raise SchemaDefinitionError(f"{where}: description must be a string")
    if "enum" in schema:
        values = schema["enum"]
        if (
            not isinstance(values, list | tuple)
            or not 0 < len(values) <= _MAX_ENUM_VALUES
            or not all(_enum_member_ok(v, kind) for v in values)
        ):
            raise SchemaDefinitionError(f"{where}: enum must hold 1-64 values of the schema type")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        _check_count(schema, key, where)
    for low, high in (("minLength", "maxLength"), ("minItems", "maxItems")):
        if low in schema and high in schema and schema[low] > schema[high]:
            raise SchemaDefinitionError(f"{where}: {low} exceeds {high}")
    for key in ("minimum", "maximum"):
        if key in schema and (not _is_number(schema[key]) or not _finite(schema[key])):
            raise SchemaDefinitionError(f"{where}: {key} must be a finite number")
    if "minimum" in schema and "maximum" in schema and schema["minimum"] > schema["maximum"]:
        raise SchemaDefinitionError(f"{where}: minimum exceeds maximum")
    if kind == "object":
        _check_object_schema(schema, where, depth, limits)
    elif kind == "array":
        if "items" not in schema:
            raise SchemaDefinitionError(f"{where}: array schema requires items")
        _check_schema_node(schema["items"], f"{where}/items", depth + 1, limits)


def _check_object_schema(
    schema: Mapping[str, Any], where: str, depth: int, limits: SchemaLimits
) -> None:
    properties = schema.get("properties")
    if not isinstance(properties, Mapping) or len(properties) > limits.max_properties:
        raise SchemaDefinitionError(f"{where}: object schema requires bounded properties")
    for key, sub in properties.items():
        if not isinstance(key, str) or not key or len(key) > MAX_NAME_LENGTH:
            raise SchemaDefinitionError(f"{where}: property names must be short strings")
        _check_schema_node(sub, f"{where}/{key}", depth + 1, limits)
    required = schema.get("required", ())
    if (
        not isinstance(required, list | tuple)
        or not all(isinstance(r, str) and r in properties for r in required)
        or len(set(required)) != len(required)
    ):
        raise SchemaDefinitionError(f"{where}: required must list declared properties once")
    if not isinstance(schema.get("additionalProperties", False), bool):
        raise SchemaDefinitionError(f"{where}: additionalProperties must be a boolean")


def _enum_member_ok(value: object, kind: str) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return _is_plain_int(value)
    if kind == "number":
        return _is_number(value) and _finite(value)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "null":
        return value is None
    return False  # objects and arrays cannot be enumerated


def _finite(value: Any) -> bool:
    return not isinstance(value, float) or math.isfinite(value)


def check_schema(schema: object, *, limits: SchemaLimits = DEFAULT_LIMITS) -> None:
    """Raise SchemaDefinitionError unless `schema` is inside the supported subset."""
    _check_schema_node(schema, "#", 0, limits)


def _safe_key(key: object) -> str:
    text = key if isinstance(key, str) else "?"
    cleaned = "".join(c if c.isascii() and c.isprintable() and c != "/" else "?" for c in text[:32])
    return cleaned


class _Walk:
    def __init__(self, limits: SchemaLimits) -> None:
        self.limits = limits
        self.violations: list[SchemaViolation] = []
        self.nodes = 0
        self.chars = 0
        self.aborted = False

    def add(self, path: tuple[str, ...], code: SchemaViolationCode) -> None:
        if len(self.violations) < MAX_VIOLATIONS:
            text = ("/" + "/".join(path))[:MAX_PATH_LENGTH]
            self.violations.append(SchemaViolation(text, code))

    def fatal(self, path: tuple[str, ...], code: SchemaViolationCode) -> None:
        self.add(path, code)
        self.aborted = True

    def enter(self, path: tuple[str, ...], depth: int) -> bool:
        if self.aborted:
            return False
        if depth > self.limits.max_depth:
            self.fatal(path, SchemaViolationCode.DEPTH)
            return False
        self.nodes += 1
        if self.nodes > self.limits.max_nodes:
            self.fatal(path, SchemaViolationCode.SIZE)
            return False
        return True

    def count_chars(self, path: tuple[str, ...], n: int) -> bool:
        self.chars += n
        if self.chars > self.limits.max_total_chars:
            self.fatal(path, SchemaViolationCode.SIZE)
            return False
        return True

    def check(
        self, schema: Mapping[str, Any], value: Any, path: tuple[str, ...], depth: int
    ) -> None:
        if not self.enter(path, depth):
            return
        kind = schema["type"]
        if not self._type_ok(kind, value, path):
            return
        if "enum" in schema and not any(
            type(value) is type(member) and value == member for member in schema["enum"]
        ):
            self.add(path, SchemaViolationCode.ENUM)
        if kind == "object":
            self._object(schema, value, path, depth)
        elif kind == "array":
            self._array(schema, value, path, depth)
        elif kind == "string":
            self._string(schema, value, path)
        elif kind in ("integer", "number"):
            if "minimum" in schema and value < schema["minimum"]:
                self.add(path, SchemaViolationCode.RANGE)
            if "maximum" in schema and value > schema["maximum"]:
                self.add(path, SchemaViolationCode.RANGE)

    def _type_ok(self, kind: str, value: Any, path: tuple[str, ...]) -> bool:
        if kind == "object":
            ok = isinstance(value, Mapping)
        elif kind == "array":
            ok = isinstance(value, list | tuple)
        elif kind == "string":
            ok = isinstance(value, str)
        elif kind == "boolean":
            ok = isinstance(value, bool)
        elif kind == "null":
            ok = value is None
        elif kind == "integer":
            ok = _is_plain_int(value)
            if ok and not -INTEGER_BOUND < value < INTEGER_BOUND:
                self.add(path, SchemaViolationCode.RANGE)
                return False
        else:
            ok = _is_number(value)
            if ok and isinstance(value, float) and not math.isfinite(value):
                self.add(path, SchemaViolationCode.NOT_FINITE)
                return False
            if ok and _is_plain_int(value) and not -INTEGER_BOUND < value < INTEGER_BOUND:
                self.add(path, SchemaViolationCode.RANGE)
                return False
        if not ok:
            self.add(path, SchemaViolationCode.TYPE)
        return ok

    def _string(self, schema: Mapping[str, Any], value: str, path: tuple[str, ...]) -> None:
        if not self.count_chars(path, len(value)):
            return
        maximum = min(schema.get("maxLength", math.inf), self.limits.max_string_length)
        if len(value) > maximum or len(value) < schema.get("minLength", 0):
            self.add(path, SchemaViolationCode.LENGTH)

    def _array(
        self, schema: Mapping[str, Any], value: list | tuple, path: tuple[str, ...], depth: int
    ) -> None:
        maximum = min(schema.get("maxItems", math.inf), self.limits.max_items)
        if len(value) > maximum or len(value) < schema.get("minItems", 0):
            self.add(path, SchemaViolationCode.ITEM_COUNT)
            if len(value) > self.limits.max_items:
                self.aborted = True
                return
        for index, item in enumerate(value):
            if self.aborted:
                return
            self.check(schema["items"], item, (*path, str(index)), depth + 1)

    def _object(
        self, schema: Mapping[str, Any], value: Mapping, path: tuple[str, ...], depth: int
    ) -> None:
        if len(value) > self.limits.max_properties:
            self.fatal(path, SchemaViolationCode.SIZE)
            return
        properties = schema["properties"]
        allow_extra = schema.get("additionalProperties", False)
        for name in schema.get("required", ()):
            if name not in value:
                self.add((*path, name), SchemaViolationCode.REQUIRED)
        for key, item in value.items():
            if self.aborted:
                return
            if not isinstance(key, str):
                self.add(path, SchemaViolationCode.KEY_TYPE)
                continue
            if not self.count_chars(path, len(key)):
                return
            child = (*path, _safe_key(key))
            if key in properties:
                self.check(properties[key], item, child, depth + 1)
            elif allow_extra:
                self.check_any(item, child, depth + 1)
            else:
                self.add(child, SchemaViolationCode.ADDITIONAL_PROPERTY)

    def check_any(self, value: Any, path: tuple[str, ...], depth: int) -> None:
        """Accept any bounded JSON value (used for additionalProperties=true)."""
        if not self.enter(path, depth):
            return
        if isinstance(value, str):
            if not self.count_chars(path, len(value)) or len(value) > self.limits.max_string_length:
                self.add(path, SchemaViolationCode.LENGTH)
        elif isinstance(value, Mapping):
            if len(value) > self.limits.max_properties:
                self.fatal(path, SchemaViolationCode.SIZE)
                return
            for key, item in value.items():
                if not isinstance(key, str):
                    self.add(path, SchemaViolationCode.KEY_TYPE)
                    continue
                if not self.count_chars(path, len(key)):
                    return
                self.check_any(item, (*path, _safe_key(key)), depth + 1)
        elif isinstance(value, list | tuple):
            if len(value) > self.limits.max_items:
                self.fatal(path, SchemaViolationCode.ITEM_COUNT)
                return
            for index, item in enumerate(value):
                self.check_any(item, (*path, str(index)), depth + 1)
        elif isinstance(value, float):
            if not math.isfinite(value):
                self.add(path, SchemaViolationCode.NOT_FINITE)
        elif _is_plain_int(value):
            if not -INTEGER_BOUND < value < INTEGER_BOUND:
                self.add(path, SchemaViolationCode.RANGE)
        elif not (isinstance(value, bool) or value is None):
            self.add(path, SchemaViolationCode.TYPE)


def validate_value(
    schema: Mapping[str, Any], value: object, *, limits: SchemaLimits = DEFAULT_LIMITS
) -> tuple[SchemaViolation, ...]:
    """Validate `value` against an already-checked schema; empty tuple means valid.

    Violations carry a sanitized path and a fixed code, never the offending value.
    """
    walk = _Walk(limits)
    walk.check(schema, value, (), 0)
    return tuple(walk.violations)


def freeze_json(value: Any) -> Any:
    """Deep read-only copy of validated JSON data (dict -> mapping proxy, list -> tuple)."""
    if isinstance(value, Mapping):
        return MappingProxyType({k: freeze_json(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(freeze_json(v) for v in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _thaw_json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_thaw_json(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    """Deterministic JSON text for digests; raises ValueError if not plain finite JSON."""
    try:
        return json.dumps(_thaw_json(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("value is not canonical JSON") from exc


def digest_arguments(arguments: Mapping[str, Any]) -> str:
    """SHA-256 hex digest binding a confirmation to exact arguments."""
    return hashlib.sha256(canonical_json(arguments).encode("utf-8")).hexdigest()


def validate_tool_name(name: object) -> str:
    if not isinstance(name, str) or len(name) > MAX_NAME_LENGTH or _NAME_RE.fullmatch(name) is None:
        raise ToolSpecError("tool name must be lowercase dotted/snake case, at most 64 characters")
    return name


def safe_name(name: object) -> str:
    """Echo-safe label: valid tool names pass through, anything else becomes a placeholder."""
    try:
        return validate_tool_name(name)
    except ToolSpecError:
        return INVALID_NAME_LABEL


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any] = field(hash=False, compare=False)
    output_schema: Mapping[str, Any] = field(hash=False, compare=False)
    permission: PermissionLevel
    environment: str
    timeout_seconds: float
    cancellable: bool = True
    idempotent: bool = False
    version: str = "1.0.0"

    def __post_init__(self) -> None:
        validate_tool_name(self.name)
        if (
            not isinstance(self.description, str)
            or not self.description.strip()
            or len(self.description) > MAX_DESCRIPTION_LENGTH
        ):
            raise ToolSpecError("description must be 1-1000 characters")
        if not isinstance(self.permission, PermissionLevel):
            raise ToolSpecError("permission must be a PermissionLevel")
        if not isinstance(self.environment, str) or _LABEL_RE.fullmatch(self.environment) is None:
            raise ToolSpecError("environment must be a short lowercase label")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= MAX_TIMEOUT_SECONDS
        ):
            raise ToolSpecError("timeout_seconds must be in (0, 3600]")
        if not isinstance(self.cancellable, bool) or not isinstance(self.idempotent, bool):
            raise ToolSpecError("cancellable and idempotent must be booleans")
        if not isinstance(self.version, str) or _VERSION_RE.fullmatch(self.version) is None:
            raise ToolSpecError("version must look like 1.2.3")
        for attr in ("input_schema", "output_schema"):
            schema = getattr(self, attr)
            try:
                check_schema(schema)
            except SchemaDefinitionError as exc:
                raise ToolSpecError(f"{attr}: {exc}") from exc
            if schema["type"] != "object":
                raise ToolSpecError(f"{attr} must describe an object")
            object.__setattr__(self, attr, freeze_json(schema))


@dataclass(frozen=True)
class ToolCall:
    """A request to run a tool. `tool_name` and `arguments` are untrusted (model output)."""

    call_id: str
    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=dict, hash=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or _CALL_ID_RE.fullmatch(self.call_id) is None:
            raise ValueError("call_id must be 1-64 characters of [A-Za-z0-9_.:-]")


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    tool_name: str
    status: ToolStatus
    output: Mapping[str, Any] | None = field(default=None, hash=False, compare=False)
    error: ToolErrorCode | None = None
    violations: tuple[SchemaViolation, ...] = ()

    def __post_init__(self) -> None:
        allowed = _ALLOWED_ERRORS[self.status]
        if self.status is ToolStatus.OK:
            if self.error is not None or self.output is None:
                raise ValueError("ok results carry an output and no error")
        elif self.error not in allowed or self.output is not None:
            raise ValueError("non-ok results carry a matching error code and no output")


class CancellationToken:
    """Cooperative cancellation flag that async tools can await or poll."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise asyncio.CancelledError


@dataclass(frozen=True)
class ToolContext:
    """The only environment a tool receives: no registry, settings, or ambient credentials."""

    call_id: str
    tool_name: str
    permission: PermissionLevel
    confirmed: bool
    timeout_seconds: float
    cancellation: CancellationToken = field(compare=False, hash=False)


class Tool(Protocol):
    spec: ToolSpec

    async def run(self, arguments: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
        """Run with validated, read-only arguments and return a mapping matching output_schema."""
        ...
