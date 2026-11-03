from __future__ import annotations

import copy
import inspect
import json
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any, TypeVar, cast

T = TypeVar("T")


class _JavaScriptUndefined:
    __slots__ = ()

    def __repr__(self) -> str:
        return "JS_UNDEFINED"


JS_UNDEFINED = _JavaScriptUndefined()
"""Sentinel for JavaScript ``undefined`` where Python ``None`` must mean ``null``."""


def now_ms() -> int:
    """Return a JavaScript-compatible Unix timestamp in milliseconds."""

    return int(time.time() * 1000)


def js_date_parse_ms(value: str) -> int | float:
    """Return ``new Date(value).getTime()`` for the ISO forms used by sessions."""

    text = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            parsed = datetime.fromisoformat(text).replace(tzinfo=UTC)
        else:
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
        milliseconds = parsed.timestamp() * 1000
    except (OverflowError, OSError, ValueError):
        return math.nan
    if not math.isfinite(milliseconds) or abs(milliseconds) > 8.64e15:
        return math.nan
    return int(milliseconds) if milliseconds.is_integer() else milliseconds


async def maybe_await(value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await cast(Awaitable[T], value)
    return value


def get_field(value: object, name: str, default: T | None = None) -> Any | T | None:
    """Read a structural field from a mapping or Python object."""

    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def get_any_field(value: object, *names: str, default: T | None = None) -> Any | T | None:
    for name in names:
        marker = object()
        result = get_field(value, name, marker)
        if result is not marker:
            return result
    return default


def clone_json(value: T) -> T:
    return copy.deepcopy(value)


def callback_name(callback: Callable[..., object]) -> str:
    return getattr(callback, "__qualname__", getattr(callback, "__name__", repr(callback)))


def js_utf16_length(value: str) -> int:
    """Return JavaScript String.length for a Python string."""

    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


def js_utf16_slice(value: str, start: int = 0, end: int | None = None) -> str:
    """Apply JavaScript String.slice indexes in UTF-16 code units."""

    raw = value.encode("utf-16-le", errors="surrogatepass")
    length = len(raw) // 2
    normalized_start = min(max(length + start if start < 0 else start, 0), length)
    normalized_end = length if end is None else min(max(length + end if end < 0 else end, 0), length)
    if normalized_end < normalized_start:
        normalized_end = normalized_start
    return raw[normalized_start * 2 : normalized_end * 2].decode("utf-16-le", errors="surrogatepass")


def js_utf16_sort_key(value: str) -> tuple[int, ...]:
    """Return the code-unit ordering used by JavaScript's default Array.sort."""

    raw = value.encode("utf-16-be", errors="surrogatepass")
    return tuple((raw[index] << 8) | raw[index + 1] for index in range(0, len(raw), 2))


def js_number_to_string(value: int | float) -> str:
    """Format a Python number using ECMAScript Number::toString thresholds."""

    try:
        number = float(value)
    except OverflowError:
        number = math.copysign(math.inf, value)
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "Infinity" if number > 0 else "-Infinity"
    if number == 0:
        return "0"

    rendered = repr(number)
    if "e" not in rendered and "E" not in rendered:
        return rendered[:-2] if rendered.endswith(".0") else rendered

    mantissa, raw_exponent = rendered.lower().split("e", 1)
    exponent = int(raw_exponent)
    sign = ""
    if mantissa.startswith("-"):
        sign, mantissa = "-", mantissa[1:]
    digits = mantissa.replace(".", "")
    decimal_position = (mantissa.find(".") if "." in mantissa else len(mantissa)) + exponent

    if 1e-6 <= abs(number) < 1e21:
        if decimal_position <= 0:
            return f"{sign}0.{('0' * -decimal_position)}{digits}"
        if decimal_position >= len(digits):
            return f"{sign}{digits}{'0' * (decimal_position - len(digits))}"
        return f"{sign}{digits[:decimal_position]}.{digits[decimal_position:]}"

    normalized_mantissa = mantissa[:-2] if mantissa.endswith(".0") else mantissa
    exponent_text = f"+{exponent}" if exponent >= 0 else str(exponent)
    return f"{sign}{normalized_mantissa}e{exponent_text}"


def js_parse_json_number(value: str) -> int | float:
    """Parse a JSON number through the IEEE-754 ``Number`` used by JSON.parse."""

    number = float(value)
    if number == 0 and value.lstrip().startswith("-"):
        return -0.0
    return int(number) if math.isfinite(number) and number.is_integer() else number


def js_to_fixed(value: int | float, digits: int) -> str:
    """Format a finite number using the rounding used by Number.toFixed()."""

    number = float(value)
    if not math.isfinite(number):
        return js_number_to_string(number)
    if not 0 <= digits <= 100:
        raise ValueError("toFixed() digits argument must be between 0 and 100")
    if abs(number) >= 1e21:
        return js_number_to_string(number)
    if number == 0:
        return "0" if digits == 0 else f"0.{'0' * digits}"
    quantum = Decimal(1).scaleb(-digits)
    with localcontext() as context:
        context.prec = 1100
        rounded = Decimal.from_float(number).quantize(quantum, rounding=ROUND_HALF_UP)
    return format(rounded, f".{digits}f")


def _js_property_key(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return js_number_to_string(value)
    return str(value)


def _array_index(value: str) -> int | None:
    if not value or (len(value) > 1 and value.startswith("0")) or not value.isascii() or not value.isdigit():
        return None
    index = int(value)
    return index if 0 <= index < 4_294_967_295 and str(index) == value else None


def js_object_items(value: Mapping[Any, T]) -> list[tuple[str, T]]:
    """Enumerate mapping entries using JavaScript Object.keys ordering."""

    converted: dict[str, T] = {}
    for key, item in value.items():
        converted[_js_property_key(key)] = item
    indexed: list[tuple[int, str, T]] = []
    ordinary: list[tuple[str, T]] = []
    for key, item in converted.items():
        index = _array_index(key)
        if index is None:
            ordinary.append((key, item))
        else:
            indexed.append((index, key, item))
    indexed.sort(key=lambda entry: entry[0])
    return [(key, item) for _index, key, item in indexed] + ordinary


_JSON_UNDEFINED = object()


def _well_formed_json_string(value: str) -> str:
    rendered = json.dumps(value, ensure_ascii=False)
    output: list[str] = []
    index = 0
    while index < len(rendered):
        code = ord(rendered[index])
        if 0xD800 <= code <= 0xDBFF and index + 1 < len(rendered):
            next_code = ord(rendered[index + 1])
            if 0xDC00 <= next_code <= 0xDFFF:
                output.append(chr(0x10000 + ((code - 0xD800) << 10) + next_code - 0xDC00))
                index += 2
                continue
        if 0xD800 <= code <= 0xDFFF:
            output.append(f"\\u{code:04x}")
        else:
            output.append(rendered[index])
        index += 1
    return "".join(output)


def js_json_stringify(value: Any, indent: int | None = None) -> str | None:
    """Serialize JSON-compatible values with JSON.stringify's core semantics."""

    gap = " " * min(max(indent or 0, 0), 10)
    active: set[int] = set()

    def encode(item: Any, level: int, *, array_item: bool = False) -> str | object:
        if item is None:
            return "null"
        if item is True:
            return "true"
        if item is False:
            return "false"
        if isinstance(item, str):
            return _well_formed_json_string(item)
        if isinstance(item, (int, float)):
            try:
                number = float(item)
            except OverflowError:
                number = math.copysign(math.inf, item)
            return "null" if not math.isfinite(number) else js_number_to_string(item)
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in active:
                raise TypeError("Converting circular structure to JSON")
            active.add(identity)
            try:
                fields: list[tuple[str, str]] = []
                for key, field_value in js_object_items(item):
                    encoded = encode(field_value, level + 1)
                    if encoded is not _JSON_UNDEFINED:
                        fields.append((_well_formed_json_string(key), cast(str, encoded)))
                if not fields:
                    return "{}"
                if not gap:
                    return "{" + ",".join(f"{key}:{encoded}" for key, encoded in fields) + "}"
                inner = ",\n".join(f"{gap * (level + 1)}{key}: {encoded}" for key, encoded in fields)
                return f"{{\n{inner}\n{gap * level}}}"
            finally:
                active.remove(identity)
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            identity = id(item)
            if identity in active:
                raise TypeError("Converting circular structure to JSON")
            active.add(identity)
            try:
                values: list[str] = []
                for child in item:
                    encoded = encode(child, level + 1, array_item=True)
                    values.append("null" if encoded is _JSON_UNDEFINED else cast(str, encoded))
                if not values:
                    return "[]"
                if not gap:
                    return "[" + ",".join(values) + "]"
                inner = ",\n".join(f"{gap * (level + 1)}{encoded}" for encoded in values)
                return f"[\n{inner}\n{gap * level}]"
            finally:
                active.remove(identity)
        return "null" if array_item else _JSON_UNDEFINED

    encoded = encode(value, 0)
    return None if encoded is _JSON_UNDEFINED else cast(str, encoded)
