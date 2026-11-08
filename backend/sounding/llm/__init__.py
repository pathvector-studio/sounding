"""LLM abstraction (docs/DESIGN.md 4.2): providers, call log, price table, budget, result reuse."""

from sounding.llm.base import (
    ERROR_CODES,
    LLMError,
    LLMResult,
    SchemaError,
    Usage,
    assert_strict_schema,
    validate_schema,
)
from sounding.llm.client import LLMClient, build_client, calls_today, day_bounds, request_hash, spend_today
from sounding.llm.pricing import PRICES, estimate_cost, price_for
from sounding.llm.providers import (
    AnthropicProvider,
    FakeProvider,
    LLMProvider,
    OpenAIProvider,
    build_provider,
)

__all__ = [
    "ERROR_CODES", "LLMError", "LLMResult", "SchemaError", "Usage", "assert_strict_schema",
    "validate_schema", "LLMClient", "build_client", "calls_today", "day_bounds", "request_hash",
    "spend_today", "PRICES", "estimate_cost", "price_for", "AnthropicProvider", "FakeProvider",
    "LLMProvider", "OpenAIProvider", "build_provider",
]
