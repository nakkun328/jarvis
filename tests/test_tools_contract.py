"""Tool contract: spec validation, JSON-Schema subset, results, digests."""

import asyncio
from types import MappingProxyType

import pytest

from backend.tools.contract import (
    CancellationToken,
    PermissionLevel,
    SchemaDefinitionError,
    SchemaLimits,
    SchemaViolationCode,
    ToolCall,
    ToolErrorCode,
    ToolResult,
    ToolSpec,
    ToolSpecError,
    ToolStatus,
    check_schema,
    digest_arguments,
    safe_name,
    validate_tool_name,
    validate_value,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 8},
        "count": {"type": "integer", "minimum": 0, "maximum": 10},
        "ratio": {"type": "number"},
        "flag": {"type": "boolean"},
        "mode": {"type": "string", "enum": ["a", "b"]},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
        "none": {"type": "null"},
        "nested": {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        },
    },
    "required": ["name"],
}


def codes(value, schema=SCHEMA, **kwargs):
    return {v.code for v in validate_value(schema, value, **kwargs)}


def make_spec(**overrides):
    fields = {
        "name": "fake.echo",
        "description": "Echo text.",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
        "output_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
        "permission": PermissionLevel.GREEN,
        "environment": "local",
        "timeout_seconds": 1.0,
    }
    fields.update(overrides)
    return ToolSpec(**fields)


def test_valid_values_pass():
    value = {
        "name": "abc",
        "count": 3,
        "ratio": 1,
        "flag": True,
        "mode": "a",
        "tags": ["x"],
        "none": None,
        "nested": {"x": 1},
    }
    assert validate_value(SCHEMA, value) == ()
    assert validate_value(SCHEMA, {"name": "a", "ratio": 1.5, "tags": ("x", "y")}) == ()


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ({}, SchemaViolationCode.REQUIRED),
        ({"name": 1}, SchemaViolationCode.TYPE),
        ({"name": ""}, SchemaViolationCode.LENGTH),
        ({"name": "x" * 9}, SchemaViolationCode.LENGTH),
        ({"name": "a", "count": True}, SchemaViolationCode.TYPE),
        ({"name": "a", "count": 1.0}, SchemaViolationCode.TYPE),
        ({"name": "a", "count": -1}, SchemaViolationCode.RANGE),
        ({"name": "a", "count": 11}, SchemaViolationCode.RANGE),
        ({"name": "a", "count": 2**70}, SchemaViolationCode.RANGE),
        ({"name": "a", "ratio": float("nan")}, SchemaViolationCode.NOT_FINITE),
        ({"name": "a", "ratio": float("inf")}, SchemaViolationCode.NOT_FINITE),
        ({"name": "a", "ratio": False}, SchemaViolationCode.TYPE),
        ({"name": "a", "flag": 1}, SchemaViolationCode.TYPE),
        ({"name": "a", "mode": "c"}, SchemaViolationCode.ENUM),
        ({"name": "a", "tags": ["a", "b", "c"]}, SchemaViolationCode.ITEM_COUNT),
        ({"name": "a", "tags": "ab"}, SchemaViolationCode.TYPE),
        ({"name": "a", "tags": [1]}, SchemaViolationCode.TYPE),
        ({"name": "a", "none": 0}, SchemaViolationCode.TYPE),
        ({"name": "a", "extra": 1}, SchemaViolationCode.ADDITIONAL_PROPERTY),
        ({"name": "a", "nested": {}}, SchemaViolationCode.REQUIRED),
        ({"name": "a", "nested": {"x": 1, "y": 2}}, SchemaViolationCode.ADDITIONAL_PROPERTY),
        ({"name": "a", 5: 1}, SchemaViolationCode.KEY_TYPE),
        ([], SchemaViolationCode.TYPE),
        ("text", SchemaViolationCode.TYPE),
    ],
)
def test_invalid_values_report_fixed_codes(value, code):
    assert code in codes(value)


def test_enum_is_type_strict():
    schema = {"type": "integer", "enum": [1, 2]}
    assert validate_value(schema, 1) == ()
    assert validate_value(schema, True)  # bool is not integer
    num = {"type": "number", "enum": [1]}
    assert validate_value(num, 1.0)  # 1.0 is a different JSON text than 1


def test_additional_properties_true_accepts_bounded_json_only():
    schema = {"type": "object", "properties": {}, "additionalProperties": True}
    assert validate_value(schema, {"k": {"a": [1, "x", None, 1.5, True]}}) == ()
    assert SchemaViolationCode.NOT_FINITE in codes({"k": float("nan")}, schema)
    assert SchemaViolationCode.TYPE in codes({"k": object()}, schema)
    assert SchemaViolationCode.TYPE in codes({"k": b"bytes"}, schema)
    assert SchemaViolationCode.TYPE in codes({"k": {1, 2}}, schema)


def test_depth_is_bounded():
    schema = {"type": "object", "properties": {}, "additionalProperties": True}
    deep: dict = {}
    cursor = deep
    for _ in range(40):
        cursor["k"] = {}
        cursor = cursor["k"]
    assert codes(deep, schema) == {SchemaViolationCode.DEPTH}


def test_cyclic_value_is_rejected_not_recursed_forever():
    schema = {"type": "object", "properties": {}, "additionalProperties": True}
    loop: dict = {}
    loop["self"] = loop
    assert SchemaViolationCode.DEPTH in codes(loop, schema)


def test_size_limits():
    schema = {"type": "object", "properties": {"s": {"type": "string"}}}
    assert SchemaViolationCode.LENGTH in codes({"s": "x" * 70_000}, schema)
    small = SchemaLimits(max_total_chars=10)
    assert SchemaViolationCode.SIZE in codes({"s": "x" * 11}, schema, limits=small)
    many = {"type": "array", "items": {"type": "integer"}}
    assert SchemaViolationCode.SIZE in codes(
        list(range(50)), many, limits=SchemaLimits(max_nodes=20)
    )
    assert SchemaViolationCode.ITEM_COUNT in codes(
        list(range(50)), many, limits=SchemaLimits(max_items=10)
    )
    open_obj = {"type": "object", "properties": {}, "additionalProperties": True}
    big = {str(i): i for i in range(300)}
    assert SchemaViolationCode.SIZE in codes(big, open_obj)


def test_violations_cap_and_sanitize_paths():
    open_schema = {"type": "object", "properties": {}}
    hostile = {f"k\n/{'x' * 100}{i}": 1 for i in range(50)}
    violations = validate_value(open_schema, hostile)
    assert len(violations) == 16
    for violation in violations:
        assert len(violation.path) <= 120
        assert "\n" not in violation.path
        assert violation.path.count("/") == 1


@pytest.mark.parametrize(
    "schema",
    [
        [],
        {},
        {"type": "any"},
        {"type": ["string", "null"]},
        {"type": "string", "pattern": ".*"},
        {"type": "string", "properties": {}},
        {"type": "array"},
        {"type": "object"},
        {"type": "object", "properties": {}, "required": ["a"]},
        {"type": "object", "properties": {"a": {"type": "null"}}, "required": ["a", "a"]},
        {"type": "object", "properties": {}, "additionalProperties": {"type": "string"}},
        {"type": "string", "minLength": 3, "maxLength": 1},
        {"type": "string", "minLength": -1},
        {"type": "integer", "minimum": float("nan")},
        {"type": "integer", "minimum": 5, "maximum": 1},
        {"type": "string", "enum": []},
        {"type": "string", "enum": [1]},
        {"type": "object", "properties": {}, "enum": [{}]},
        {"type": "object", "properties": {"": {"type": "null"}}},
    ],
)
def test_bad_schema_definitions_rejected(schema):
    with pytest.raises(SchemaDefinitionError):
        check_schema(schema)


def test_schema_depth_is_bounded():
    schema = {"type": "string"}
    for _ in range(12):
        schema = {"type": "array", "items": schema}
    with pytest.raises(SchemaDefinitionError):
        check_schema(schema)


def test_valid_schema_definition():
    check_schema(SCHEMA)


@pytest.mark.parametrize("name", ["a", "fs.read_file", "web.search2", "a_b.c_d"])
def test_valid_names(name):
    assert validate_tool_name(name) == name


@pytest.mark.parametrize(
    "name", ["", "Fs.read", "1abc", "a..b", ".a", "a.", "a-b", "a b", "a" * 65, "é", None, 3]
)
def test_invalid_names(name):
    with pytest.raises(ToolSpecError):
        validate_tool_name(name)
    assert safe_name(name) == "<invalid>"


def test_spec_defaults_and_immutability():
    spec = make_spec()
    assert spec.cancellable is True and spec.idempotent is False and spec.version == "1.0.0"
    assert isinstance(spec.input_schema, MappingProxyType)
    with pytest.raises(AttributeError):
        spec.name = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        spec.input_schema["type"] = "string"  # type: ignore[index]


def test_spec_snapshots_schema_input():
    schema = {"type": "object", "properties": {}}
    spec = make_spec(input_schema=schema)
    schema["properties"]["late"] = {"type": "string"}
    assert "late" not in spec.input_schema["properties"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "Bad"},
        {"description": "  "},
        {"description": "x" * 1001},
        {"permission": "green"},
        {"environment": "Local Machine"},
        {"timeout_seconds": 0},
        {"timeout_seconds": -1},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": 4000},
        {"timeout_seconds": True},
        {"cancellable": "yes"},
        {"version": "v1"},
        {"input_schema": {"type": "string"}},
        {"output_schema": {"type": "object"}},
    ],
)
def test_invalid_specs_rejected(overrides):
    with pytest.raises(ToolSpecError):
        make_spec(**overrides)


def test_call_id_validation():
    assert ToolCall("call-1", "fake.echo").arguments == {}
    for bad in ["", "a b", "x" * 65, "line\nbreak"]:
        with pytest.raises(ValueError):
            ToolCall(bad, "fake.echo")


def test_result_invariants():
    ok = ToolResult("c", "t", ToolStatus.OK, output={"a": 1})
    assert ok.error is None
    with pytest.raises(ValueError):
        ToolResult("c", "t", ToolStatus.OK)
    with pytest.raises(ValueError):
        ToolResult("c", "t", ToolStatus.ERROR)
    with pytest.raises(ValueError):
        ToolResult("c", "t", ToolStatus.TIMEOUT, error=ToolErrorCode.CANCELLED)
    with pytest.raises(ValueError):
        ToolResult("c", "t", ToolStatus.DENIED, output={}, error=ToolErrorCode.PERMISSION_DENIED)
    assert ToolResult("c", "t", ToolStatus.DENIED, error=ToolErrorCode.CONFIRMATION_REQUIRED)


def test_error_codes_are_a_fixed_vocabulary():
    assert {e.value for e in ToolErrorCode} == {
        "unknown_tool",
        "tool_unavailable",
        "invalid_arguments",
        "permission_denied",
        "confirmation_required",
        "timeout",
        "cancelled",
        "internal_error",
        "invalid_output",
    }


def test_digest_is_canonical_and_exact():
    assert digest_arguments({"a": 1, "b": [1, 2]}) == digest_arguments({"b": [1, 2], "a": 1})
    assert digest_arguments({"a": 1}) != digest_arguments({"a": 2})
    assert digest_arguments({"a": 1}) != digest_arguments({"a": 1.0})
    assert digest_arguments({"a": (1, 2)}) == digest_arguments({"a": [1, 2]})
    with pytest.raises(ValueError):
        digest_arguments({"a": float("nan")})
    with pytest.raises(ValueError):
        digest_arguments({"a": object()})
    loop: dict = {}
    loop["x"] = loop
    with pytest.raises(ValueError):
        digest_arguments(loop)


def test_cancellation_token():
    async def run():
        token = CancellationToken()
        assert not token.cancelled
        token.raise_if_cancelled()
        waiter = asyncio.ensure_future(token.wait())
        await asyncio.sleep(0)
        assert not waiter.done()
        token.cancel()
        await asyncio.wait_for(waiter, 1)
        with pytest.raises(asyncio.CancelledError):
            token.raise_if_cancelled()

    asyncio.run(run())
