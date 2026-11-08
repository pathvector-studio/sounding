"""Types shared by the LLM providers: usage, result, fixed error codes and a small JSON Schema check.

Error codes are the only error text that is stored or logged (``llm_calls.error``, ``job_runs``):
SDK exception messages can echo request content, so they are never kept.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

Tier = Literal["cheap", "main"]
TIERS: tuple[str, ...] = ("cheap", "main")

# Every code an LLMError can carry (llm_calls.error holds one of these, or None).
ERROR_CODES = (
    "schema_mismatch",
    "refusal",
    "max_tokens",
    "rate_limited",
    "connection_error",
    "config_error",
    "budget_exceeded",
)  # plus "http_<status>"


@dataclass(frozen=True)
class Usage:
    """Token counts. ``input_tokens`` excludes the cache read/write tokens (priced separately)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
        )


@dataclass(frozen=True)
class RawResponse:
    text: str
    usage: Usage


@dataclass
class LLMResult:
    data: Any  # parsed and schema-checked JSON
    provider: str
    model: str
    tier: str
    usage: Usage
    cost_usd: Decimal
    duration_ms: int
    reused: bool = False  # True: taken from an earlier ok call, nothing was billed
    request_hash: str | None = None
    call_id: int | None = None  # llm_calls.id of the row written for this call

    @property
    def cache_write_hit(self) -> bool:
        return self.usage.cache_write_tokens > 0

    @property
    def cache_read_hit(self) -> bool:
        return self.usage.cache_read_tokens > 0


class LLMError(Exception):
    """A failed LLM call. ``code`` is one of ERROR_CODES or ``http_<status>``; ``usage`` is what was billed."""

    def __init__(self, code: str, *, usage: Usage | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.usage = usage or Usage()


class SchemaError(ValueError):
    """The response is not valid JSON or does not match the schema (message names the path only)."""


_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "null": (type(None),),
    "object": (dict,),
    "array": (list,),
}


def _type_ok(value: Any, name: str) -> bool:
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES[name])


def validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Check ``value`` against the JSON Schema subset we use.

    Supported: type (name or list), enum, properties, required, additionalProperties (false),
    items. Raises SchemaError with the path of the first mismatch (never the value itself).
    """
    expected = schema.get("type")
    if expected is not None:
        names = [expected] if isinstance(expected, str) else list(expected)
        if not any(_type_ok(value, n) for n in names):
            raise SchemaError(f"{path}: expected {'|'.join(names)}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path}: not in enum")
    if isinstance(value, dict):
        props: dict[str, Any] = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise SchemaError(f"{path}.{key}: required")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(props))
            if extra:
                raise SchemaError(f"{path}.{extra[0]}: not allowed")
        for key, sub in props.items():
            if key in value:
                validate_schema(value[key], sub, f"{path}.{key}")
    elif isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            validate_schema(item, schema["items"], f"{path}[{i}]")


def parse_and_validate(text: str, schema: dict[str, Any]) -> Any:
    try:
        data = json.loads(text)
    except ValueError:
        raise SchemaError("$: not valid JSON") from None
    validate_schema(data, schema)
    return data


_UNSUPPORTED = {"minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems", "pattern", "$ref"}


def assert_strict_schema(schema: dict[str, Any], path: str = "$") -> None:
    """Raise ValueError unless the schema is within what both providers' structured output accepts.

    Every object has ``additionalProperties: false`` and lists all its properties in ``required``;
    no length/range keywords and no ``$ref`` (recursive schemas). For use in tests of prompts/schemas.
    """
    bad = _UNSUPPORTED & schema.keys()
    if bad:
        raise ValueError(f"{path}: unsupported keyword {sorted(bad)[0]}")
    types = schema.get("type")
    names = [types] if isinstance(types, str) else list(types or [])
    if "object" in names or "properties" in schema:
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is not False:
            raise ValueError(f"{path}: additionalProperties must be false")
        if set(schema.get("required", [])) != set(props):
            raise ValueError(f"{path}: required must list every property")
        for key, sub in props.items():
            assert_strict_schema(sub, f"{path}.{key}")
    if "items" in schema:
        assert_strict_schema(schema["items"], f"{path}[]")


@dataclass
class _Request:  # what a FakeProvider records
    purpose: str
    tier: str
    model: str
    system: str
    user: str
    schema: dict[str, Any]
    max_tokens: int
    extra: dict[str, Any] = field(default_factory=dict)
