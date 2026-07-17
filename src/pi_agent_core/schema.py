from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping
from typing import Any, cast

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import ValidationError

from ._utils import get_field, js_json_stringify, js_number_to_string

_DECIMAL_NUMBER = re.compile(r"^[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)$")
_RADIX_NUMBER = re.compile(r"^0([xX][0-9a-fA-F]+|[bB][01]+|[oO][0-7]+)$")


def _is_finite_number(_checker: object, value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_finite_integer(_checker: object, value: object) -> bool:
    if not _is_finite_number(_checker, value):
        return False
    assert isinstance(value, (int, float))
    return float(value).is_integer()


_TYPEBOX_TYPE_CHECKER = Draft202012Validator.TYPE_CHECKER.redefine_many(
    {"number": _is_finite_number, "integer": _is_finite_integer}
)
_TypeBoxCompatibleValidator = cast(Any, validators.extend)(Draft202012Validator, type_checker=_TYPEBOX_TYPE_CHECKER)


def _schema_types(schema: Mapping[str, Any]) -> list[str]:
    schema_type = schema.get("type")
    if isinstance(schema_type, str):
        return [schema_type]
    if isinstance(schema_type, list):
        return [item for item in schema_type if isinstance(item, str)]
    return []


def _matches_json_type(value: Any, schema_type: str) -> bool:
    if schema_type == "number":
        # JavaScript's `typeof NaN` and `typeof Infinity` are both "number".
        # The schema validator is responsible for rejecting them afterwards.
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "integer":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and float(value).is_integer()
        )
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "null":
        return value is None
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "object":
        return isinstance(value, Mapping)
    return False


def _parse_js_number(value: str) -> int | float | None:
    stripped = value.strip()
    if not stripped:
        return None
    try:
        radix_match = _RADIX_NUMBER.fullmatch(stripped)
        if radix_match:
            prefix = stripped[1].lower()
            return int(stripped[2:], {"x": 16, "b": 2, "o": 8}[prefix])
        if not _DECIMAL_NUMBER.fullmatch(stripped):
            return None
        number = float(stripped)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if number == 0 and stripped.startswith("-"):
        return -0.0
    return int(number) if number.is_integer() else number


def _js_string(value: int | float | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return js_number_to_string(value)


def _normalize_validation_schema(value: Any) -> Any:
    """Translate TypeBox's legacy tuple schema into Draft 2020-12 form."""

    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if key == "items" and isinstance(item, list):
                normalized["prefixItems"] = [_normalize_validation_schema(child) for child in item]
            else:
                normalized[str(key)] = _normalize_validation_schema(item)
        return normalized
    if isinstance(value, list):
        return [_normalize_validation_schema(item) for item in value]
    return value


def _coerce_primitive(value: Any, schema_type: str) -> Any:
    if schema_type in {"number", "integer"}:
        if value is None:
            return 0
        if isinstance(value, str):
            parsed = _parse_js_number(value)
            if parsed is not None and (schema_type == "number" or float(parsed).is_integer()):
                return parsed
        if isinstance(value, bool):
            return 1 if value else 0
        return value
    if schema_type == "boolean":
        if value is None:
            return False
        if value == "true":
            return True
        if value == "false":
            return False
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value == 1:
                return True
            if value == 0:
                return False
        return value
    if schema_type == "string":
        if value is None:
            return ""
        if isinstance(value, (int, float, bool)):
            return _js_string(value)
        return value
    if schema_type == "null" and (value == "" or value == 0 or value is False):
        return None
    return value


def _changed(before: Any, after: Any) -> bool:
    return type(before) is not type(after) or before != after


def _coerce_union(value: Any, schemas: list[Any]) -> Any:
    for raw_schema in schemas:
        if not isinstance(raw_schema, Mapping):
            continue
        schema = cast(Mapping[str, Any], raw_schema)
        candidate = _coerce(copy.deepcopy(value), schema)
        try:
            if _TypeBoxCompatibleValidator(_normalize_validation_schema(schema)).is_valid(candidate):
                return candidate
        except (TypeError, ValueError):
            continue
    return value


def _coerce(value: Any, schema: Mapping[str, Any]) -> Any:
    next_value = value

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for nested in all_of:
            if isinstance(nested, Mapping):
                next_value = _coerce(next_value, cast(Mapping[str, Any], nested))

    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        next_value = _coerce_union(next_value, any_of)
    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        next_value = _coerce_union(next_value, one_of)

    schema_types = _schema_types(schema)
    matches_union_member = len(schema_types) > 1 and any(
        _matches_json_type(next_value, schema_type) for schema_type in schema_types
    )
    if schema_types and not matches_union_member:
        for schema_type in schema_types:
            candidate = _coerce_primitive(next_value, schema_type)
            if _changed(next_value, candidate):
                next_value = candidate
                break

    if "object" in schema_types and isinstance(next_value, Mapping):
        result = dict(next_value)
        properties = schema.get("properties")
        defined = set(properties) if isinstance(properties, Mapping) else set()
        if isinstance(properties, Mapping):
            for key, property_schema in properties.items():
                if key in result and isinstance(property_schema, Mapping):
                    result[key] = _coerce(result[key], cast(Mapping[str, Any], property_schema))
        additional = schema.get("additionalProperties")
        if isinstance(additional, Mapping):
            for key in result.keys() - defined:
                result[key] = _coerce(result[key], cast(Mapping[str, Any], additional))
        next_value = result

    if "array" in schema_types and isinstance(next_value, list):
        result_list = list(next_value)
        items = schema.get("items")
        if isinstance(items, list):
            for index, item_schema in enumerate(items[: len(result_list)]):
                if isinstance(item_schema, Mapping):
                    result_list[index] = _coerce(result_list[index], cast(Mapping[str, Any], item_schema))
        elif isinstance(items, Mapping):
            result_list = [_coerce(item, cast(Mapping[str, Any], items)) for item in result_list]
        next_value = result_list

    return next_value


def _format_path(error: ValidationError, required_property: str | None = None) -> str:
    path = ".".join(str(part) for part in error.absolute_path)
    if required_property:
        path = f"{path}.{required_property}" if path else required_property
    return path or "root"


def _typebox_error_message(error: ValidationError, missing: list[str] | None = None) -> str:
    keyword = str(error.validator)
    limit: Any = cast(Any, error.validator_value)
    if keyword == "required":
        return f"must have required properties {', '.join(missing or [])}"
    if keyword == "type":
        expected = limit if isinstance(limit, str) else ",".join(str(item) for item in limit)
        return f"must be {expected}"
    if keyword == "additionalProperties":
        return "must not have additional properties"
    if keyword == "minItems":
        return f"must not have fewer than {limit} items"
    if keyword == "maxItems":
        return f"must not have more than {limit} items"
    if keyword == "minLength":
        return f"must not have fewer than {limit} characters"
    if keyword == "maxLength":
        return f"must not have more than {limit} characters"
    if keyword == "minProperties":
        return f"must not have fewer than {limit} properties"
    if keyword == "maxProperties":
        return f"must not have more than {limit} properties"
    if keyword == "minimum":
        return f"must be >= {js_number_to_string(limit)}"
    if keyword == "maximum":
        return f"must be <= {js_number_to_string(limit)}"
    if keyword == "exclusiveMinimum":
        return f"must be > {js_number_to_string(limit)}"
    if keyword == "exclusiveMaximum":
        return f"must be < {js_number_to_string(limit)}"
    if keyword == "multipleOf":
        return f"must be a multiple of {js_number_to_string(limit)}"
    if keyword == "pattern":
        return f'must match pattern "{limit}"'
    if keyword == "enum":
        return "must be equal to one of the allowed values"
    if keyword == "const":
        return "must be equal to constant"
    if keyword == "anyOf":
        return "must match a schema in anyOf"
    if keyword == "oneOf":
        return "must match exactly one schema in oneOf"
    if keyword == "not":
        return "must NOT be valid"
    if keyword == "uniqueItems":
        return "must not have duplicate items"
    if keyword == "contains":
        return "must contain at least 1 valid item(s)"
    return error.message


def _flatten_validation_error(error: ValidationError) -> list[ValidationError]:
    nested = [child for context in error.context for child in _flatten_validation_error(context)]
    return [*nested, error]


def _format_validation_errors(errors: list[ValidationError]) -> str:
    flattened = [item for error in errors for item in _flatten_validation_error(error)]
    lines: list[str] = []
    handled_required: set[tuple[object, ...]] = set()
    for error in flattened:
        if error.validator == "required" and isinstance(error.instance, Mapping):
            group_key = (*tuple(error.absolute_schema_path)[:-1], *tuple(error.absolute_path))
            if group_key in handled_required:
                continue
            handled_required.add(group_key)
            required = error.validator_value if isinstance(error.validator_value, list) else []
            missing = [str(item) for item in required if item not in error.instance]
            first = missing[0] if missing else None
            lines.append(f"  - {_format_path(error, first)}: {_typebox_error_message(error, missing)}")
            continue
        lines.append(f"  - {_format_path(error)}: {_typebox_error_message(error)}")
    return "\n".join(lines) or "Unknown validation error"


def validate_tool_arguments(tool: object, tool_call: Mapping[str, Any]) -> Any:
    raw_schema: object = get_field(tool, "parameters", {})
    if not isinstance(raw_schema, Mapping):
        raise TypeError("Tool parameters must be a JSON Schema mapping")
    schema = cast(Mapping[str, Any], raw_schema)
    validation_schema = _normalize_validation_schema(schema)
    original = copy.deepcopy(tool_call.get("arguments", {}))
    arguments = _coerce(original, schema)
    errors = list(_TypeBoxCompatibleValidator(validation_schema).iter_errors(arguments))
    if not errors:
        return arguments
    lines = _format_validation_errors(errors)
    name = tool_call.get("name", get_field(tool, "name", "unknown"))
    rendered = js_json_stringify(original, indent=2) or "undefined"
    raise ValueError(f'Validation failed for tool "{name}":\n{lines}\n\nReceived arguments:\n{rendered}')


validateToolArguments = validate_tool_arguments
