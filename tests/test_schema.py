from __future__ import annotations

import math

import pytest

from pi_agent_core import validateToolArguments


def _case(schema: dict[str, object], value: object) -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "name": "echo",
            "parameters": {
                "type": "object",
                "properties": {"value": schema},
                "required": ["value"],
            },
        },
        {"type": "toolCall", "id": "tool-1", "name": "echo", "arguments": {"value": value}},
    )


@pytest.mark.parametrize(
    ("schema", "value", "expected"),
    [
        ({"type": "number"}, "42", 42),
        ({"type": "number"}, True, 1),
        ({"type": "number"}, None, 0),
        ({"type": "integer"}, "42", 42),
        ({"type": "boolean"}, "true", True),
        ({"type": "boolean"}, "false", False),
        ({"type": "boolean"}, 1, True),
        ({"type": "boolean"}, 0, False),
        ({"type": "string"}, None, ""),
        ({"type": "string"}, True, "true"),
        ({"type": "null"}, "", None),
        ({"type": "null"}, 0, None),
        ({"type": "null"}, False, None),
        ({"type": ["number", "string"]}, "1", "1"),
        ({"type": ["boolean", "number"]}, "1", 1),
    ],
)
def test_plain_json_schema_uses_upstream_primitive_coercion(
    schema: dict[str, object], value: object, expected: object
) -> None:
    tool, call = _case(schema, value)
    assert validateToolArguments(tool, call) == {"value": expected}


@pytest.mark.parametrize(
    ("schema", "value"),
    [
        ({"type": "boolean"}, "1"),
        ({"type": "boolean"}, "0"),
        ({"type": "null"}, "null"),
        ({"type": "integer"}, "42.1"),
    ],
)
def test_plain_json_schema_rejects_invalid_coercions(schema: dict[str, object], value: object) -> None:
    tool, call = _case(schema, value)
    with pytest.raises(ValueError, match='Validation failed for tool "echo"'):
        validateToolArguments(tool, call)


def test_nested_combinators_arrays_and_additional_properties_are_coerced_without_mutating_input() -> None:
    schema = {
        "type": "object",
        "properties": {
            "all": {"allOf": [{"type": "number"}, {"minimum": 1}]},
            "choice": {"anyOf": [{"type": "integer"}, {"type": "boolean"}]},
            "items": {"type": "array", "items": {"type": "boolean"}},
        },
        "additionalProperties": {"type": "number"},
        "required": ["all", "choice", "items"],
    }
    arguments = {"all": "2", "choice": "3", "items": ["true", 0], "extra": "4"}
    tool = {"name": "nested", "parameters": schema}
    call = {"name": "nested", "arguments": arguments}

    assert validateToolArguments(tool, call) == {
        "all": 2,
        "choice": 3,
        "items": [True, False],
        "extra": 4,
    }
    assert arguments == {"all": "2", "choice": "3", "items": ["true", 0], "extra": "4"}


def test_validation_error_reports_nested_required_path_and_received_arguments() -> None:
    tool = {
        "name": "nested",
        "parameters": {
            "type": "object",
            "properties": {
                "config": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                }
            },
            "required": ["config"],
        },
    }
    with pytest.raises(ValueError) as captured:
        validateToolArguments(tool, {"name": "nested", "arguments": {"config": {}}})
    assert "config.path" in str(captured.value)
    assert "must have required properties path" in str(captured.value)
    assert '"config": {}' in str(captured.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0x10", 16),
        ("0X10", 16),
        ("0b10", 2),
        ("0o10", 8),
        ("+.5", 0.5),
        ("1.", 1),
        ("1e2", 100),
        ("9007199254740993", 9007199254740992),
    ],
)
def test_number_coercion_matches_javascript_number(raw: str, expected: int | float) -> None:
    tool, call = _case({"type": "number"}, raw)
    assert validateToolArguments(tool, call) == {"value": expected}


@pytest.mark.parametrize("raw", ["+0x10", "-0x10", "1_000", "NaN", "Infinity", "0b12"])
def test_number_coercion_rejects_strings_javascript_number_rejects_or_nonfinite(raw: str) -> None:
    tool, call = _case({"type": "number"}, raw)
    with pytest.raises(ValueError):
        validateToolArguments(tool, call)


def test_negative_zero_and_legacy_tuple_items_are_preserved_and_coerced() -> None:
    number_tool, number_call = _case({"type": "number"}, "-0")
    result = validateToolArguments(number_tool, number_call)
    assert result["value"] == 0
    assert math.copysign(1, result["value"]) == -1

    tool = {
        "name": "tuple",
        "parameters": {
            "type": "array",
            "items": [{"type": "integer"}, {"type": "boolean"}],
        },
    }
    call = {"name": "tuple", "arguments": ["1", "true", "unchanged"]}
    assert validateToolArguments(tool, call) == [1, True, "unchanged"]


def test_numeric_string_conversion_uses_javascript_thresholds() -> None:
    tool, call = _case({"type": "string"}, 1e21)
    assert validateToolArguments(tool, call) == {"value": "1e+21"}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_typebox_number_semantics_reject_nonfinite_values(value: float) -> None:
    tool, call = _case({"type": "number"}, value)
    with pytest.raises(ValueError, match="Validation failed"):
        validateToolArguments(tool, call)
