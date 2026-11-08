import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx2
import pytest
from sqlalchemy import text

from sounding import llm
from sounding.config import Settings
from sounding.db import get_engine, get_session_factory
from sounding.llm import (
    AnthropicProvider,
    FakeProvider,
    LLMClient,
    LLMError,
    OpenAIProvider,
    Usage,
    assert_strict_schema,
    build_provider,
    estimate_cost,
    validate_schema,
)
from sounding.llm import pricing
from sounding.llm.base import RawResponse
from sounding.models import LlmCall
from sounding.text import mask_addresses

KEY = "test-llm-key-not-real-0123456789"
SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "n": {"type": "integer"}},
    "required": ["summary", "n"],
    "additionalProperties": False,
}
GOOD = {"summary": "hello", "n": 1}
CALL = dict(purpose="state", tier="main", system="SYS", user="USER", schema=SCHEMA, prompt_version="p1", language="en")


@pytest.fixture
def sf(session):  # session fixture truncates the tables first
    return get_session_factory()


def client(provider, sf, *, budget="1.00", clock=None):
    return LLMClient(provider, sf, tz="Asia/Tokyo", daily_budget_usd=Decimal(budget), default_max_tokens=500, clock=clock)


def rows(sf):
    with sf() as s:
        return s.query(LlmCall).order_by(LlmCall.id).all()


# --- schema validator -----------------------------------------------------------------------------

def test_validate_schema_subset():
    validate_schema(GOOD, SCHEMA)
    for bad in ({"summary": "x"}, {"summary": "x", "n": "1"}, {"summary": "x", "n": True}, {"summary": "x", "n": 1, "z": 1}, [], "x"):
        with pytest.raises(llm.SchemaError):
            validate_schema(bad, SCHEMA)
    validate_schema(None, {"type": ["string", "null"]})
    validate_schema([1, 2], {"type": "array", "items": {"type": "integer"}})
    with pytest.raises(llm.SchemaError):
        validate_schema([1, "a"], {"type": "array", "items": {"type": "integer"}})
    with pytest.raises(llm.SchemaError, match="enum"):
        validate_schema("c", {"type": "string", "enum": ["a", "b"]})


def test_assert_strict_schema():
    assert_strict_schema(SCHEMA)
    with pytest.raises(ValueError):
        assert_strict_schema({"type": "object", "properties": {"a": {"type": "string"}}, "required": []})
    with pytest.raises(ValueError):
        assert_strict_schema({"type": "object", "properties": {"a": {"type": "string", "maxLength": 3}}, "required": ["a"], "additionalProperties": False})


# --- prices ---------------------------------------------------------------------------------------

def test_cost_with_cache_tokens():
    u = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_write_tokens=200_000, cache_read_tokens=500_000)
    # 4.00 + 2.00 + 1.00 + 0.10
    assert estimate_cost("anthropic", "claude-opus-5-5", u) == Decimal("7.100000")
    assert estimate_cost("anthropic", "claude-haiku-5-5", Usage(1_000_000, 1_000_000, 1_000_000, 1_000_000)) == Decimal("0.735000")
    assert estimate_cost("anthropic", "claude-sonnet-5-5", Usage(output_tokens=1_000_000)) == Decimal("10.000000")


def test_unknown_model_uses_highest_prices_and_warns_once(caplog):
    pricing._warned.clear()
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    with caplog.at_level(logging.WARNING, logger="sounding.llm.pricing"):
        a = estimate_cost("anthropic", "claude-future-9", u)  # same provider -> highest anthropic
        b = estimate_cost("openai", "some-model", u)  # openai has no entries -> highest of the table
        c = estimate_cost("openai", "some-model", u)
    assert a == b == c == Decimal("24.000000")
    warned = [r.getMessage() for r in caplog.records]
    assert len(warned) == 2 and any("claude-future-9" in m for m in warned) and any("some-model" in m for m in warned)


# --- fake provider: retry, errors, cost -------------------------------------------------------------

def test_success_logs_usage_and_cost(sf):
    fake = FakeProvider(GOOD, usage=Usage(1000, 200, 300, 400))
    r = client(fake, sf).complete(**CALL)
    assert r.data == GOOD and not r.reused and r.model == "claude-opus-5-5"
    assert r.cache_write_hit and r.cache_read_hit
    expected = estimate_cost("anthropic", "claude-opus-5-5", Usage(1000, 200, 300, 400))
    assert r.cost_usd == expected
    (row,) = rows(sf)
    assert (row.status, row.error, row.purpose, row.provider, row.tier) == ("ok", None, "state", "fake", "main")
    assert (row.input_tokens, row.output_tokens, row.cache_write_tokens, row.cache_read_tokens) == (1000, 200, 300, 400)
    assert row.cost_usd == expected and row.response_json == GOOD and row.request_hash == r.request_hash
    assert fake.requests[0].max_tokens == 500 and fake.requests[0].purpose == "state"


def test_invalid_then_valid_retries_once_and_sums_usage(sf):
    fake = FakeProvider(["not json", GOOD], usage=Usage(100, 10))
    r = client(fake, sf).complete(**CALL)
    assert r.data == GOOD and fake.calls == 2 and r.usage == Usage(200, 20)


@pytest.mark.parametrize("bad", ["not json", {"summary": "x"}, {"summary": "x", "n": 1, "extra": 1}])
def test_schema_mismatch_retries_once_then_fails_and_is_logged(sf, bad):
    fake = FakeProvider(bad, usage=Usage(100, 10))
    with pytest.raises(LLMError) as ei:
        client(fake, sf).complete(**CALL)
    assert ei.value.code == "schema_mismatch" and fake.calls == 2
    (row,) = rows(sf)
    assert (row.status, row.error, row.response_json) == ("failed", "schema_mismatch", None)
    assert row.input_tokens == 200 and row.cost_usd > 0  # both attempts were billed


@pytest.mark.parametrize("code", ["refusal", "max_tokens", "rate_limited", "http_500", "connection_error"])
def test_provider_failures_are_logged_without_retry(sf, code):
    fake = FakeProvider(LLMError(code, usage=Usage(50, 5)))
    with pytest.raises(LLMError) as ei:
        client(fake, sf).complete(**CALL)
    assert ei.value.code == code and fake.calls == 1
    (row,) = rows(sf)
    assert (row.status, row.error, row.input_tokens) == ("failed", code, 50)


def test_failed_calls_are_not_reused(sf):
    fake = FakeProvider([LLMError("http_500"), GOOD])
    c = client(fake, sf)
    with pytest.raises(LLMError):
        c.complete(**CALL)
    assert c.complete(**CALL).data == GOOD and fake.calls == 2


# --- reuse ------------------------------------------------------------------------------------------

def test_reuse_by_hash_does_not_call_provider_or_add_cost(sf):
    fake = FakeProvider(GOOD)
    c = client(fake, sf)
    first = c.complete(**CALL)
    second = c.complete(**CALL, conversation_id=None)
    assert fake.calls == 1 and second.reused and second.data == GOOD
    assert second.cost_usd == 0 and second.usage == Usage() and second.request_hash == first.request_hash
    ok, reused = rows(sf)
    assert (ok.status, reused.status) == ("ok", "reused") and reused.cost_usd == 0 and reused.response_json is None
    with sf() as s:
        assert llm.spend_today(s, "Asia/Tokyo") == first.cost_usd


@pytest.mark.parametrize(
    "change", [{"user": "USER2"}, {"system": "SYS2"}, {"prompt_version": "p2"}, {"language": "ja"}, {"tier": "cheap"},
               {"schema": {**SCHEMA, "required": ["n", "summary"], "description": "x"}}]
)
def test_any_input_change_misses_the_cache(sf, change):
    fake = FakeProvider(GOOD)
    c = client(fake, sf)
    c.complete(**CALL)
    c.complete(**{**CALL, **change})
    assert fake.calls == 2


def test_schema_key_order_does_not_change_the_hash(sf):
    fake = FakeProvider(GOOD)
    c = client(fake, sf)
    c.complete(**CALL)
    reordered = {"additionalProperties": False, "required": ["summary", "n"], "properties": SCHEMA["properties"], "type": "object"}
    assert c.complete(**{**CALL, "schema": reordered}).reused and fake.calls == 1


def test_model_is_part_of_the_hash(sf):
    a = FakeProvider(GOOD, models={"main": "claude-opus-5-5", "cheap": "claude-haiku-5-5"})
    b = FakeProvider(GOOD, models={"main": "claude-sonnet-5-5", "cheap": "claude-haiku-5-5"})
    client(a, sf).complete(**CALL)
    assert not client(b, sf).complete(**CALL).reused


def test_reuse_works_even_over_budget(sf):
    fake = FakeProvider(GOOD, usage=Usage(1_000_000, 0))
    c = client(fake, sf, budget="1.00")
    c.complete(**CALL)  # costs 4.00, over budget now
    assert c.complete(**CALL).reused
    with pytest.raises(LLMError, match="budget_exceeded"):
        c.complete(**{**CALL, "user": "other"})


# --- budget -----------------------------------------------------------------------------------------

def test_budget_exceeded_blocks_the_call_without_a_row(sf):
    fake = FakeProvider(GOOD, usage=Usage(250_000, 0))  # $1.00 on opus
    c = client(fake, sf, budget="1.00")
    c.complete(**CALL)  # spend 1.00 == budget: reached
    with pytest.raises(LLMError) as ei:
        c.complete(**{**CALL, "user": "other"})
    assert ei.value.code == "budget_exceeded" and fake.calls == 1 and len(rows(sf)) == 1


def test_budget_zero_is_unlimited(sf):
    fake = FakeProvider(GOOD, usage=Usage(10_000_000, 0))
    c = client(fake, sf, budget="0")
    for i in range(3):
        c.complete(**{**CALL, "user": f"u{i}"})
    assert fake.calls == 3


def test_budget_counts_failed_calls_and_only_today_in_tz(sf):
    # Asia/Tokyo: 2026-11-08 00:00 JST == 2026-11-07 15:00 UTC
    now = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)  # 19:00 JST on the 8th
    start, end = llm.day_bounds("Asia/Tokyo", now)
    assert start == datetime(2026, 11, 7, 15, 0, tzinfo=UTC) and end == start + timedelta(days=1)
    with sf() as s:
        def add(at, cost, status="ok"):
            s.add(LlmCall(created_at=at, purpose="p", provider="x", model="m", tier="main", request_hash="h", status=status, cost_usd=Decimal(cost)))
        add(start - timedelta(seconds=1), "5")  # yesterday in JST (still the 7th in UTC)
        add(start, "0.30")  # first instant of today
        add(end - timedelta(seconds=1), "0.20", "failed")
        add(end, "9")  # tomorrow
        s.commit()
        assert llm.spend_today(s, "Asia/Tokyo", now) == Decimal("0.50")
        assert llm.spend_today(s, "UTC", now) == Decimal("9.20")  # a different calendar day in UTC: 0.20 + 9
    fake = FakeProvider(GOOD)
    c = client(fake, sf, budget="0.50", clock=lambda: now)
    with pytest.raises(LLMError, match="budget_exceeded"):
        c.complete(**CALL)
    c2 = client(fake, sf, budget="0.51", clock=lambda: now)
    assert c2.complete(**CALL).data == GOOD


def test_config_error_for_unset_model_makes_no_call_or_row(sf):
    fake = FakeProvider(GOOD, models={"main": "change-me-main-model", "cheap": ""})
    for tier in ("main", "cheap"):
        with pytest.raises(LLMError) as ei:
            client(fake, sf).complete(**{**CALL, "tier": tier})
        assert ei.value.code == "config_error"
    assert fake.calls == 0 and rows(sf) == []


# --- Anthropic (SDK against canned HTTP) ------------------------------------------------------------

def _anthropic_body(text_, *, stop="end_turn", usage=None):
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text_}], "stop_reason": stop, "stop_sequence": None,
        "usage": usage or {"input_tokens": 1000, "output_tokens": 200, "cache_creation_input_tokens": 300, "cache_read_input_tokens": 400},
    }


def _provider(kind, handler, **kw):
    http = httpx2.Client(transport=httpx2.MockTransport(handler))
    models = kw.pop("models", {"main": "claude-opus-5-5", "cheap": "claude-haiku-5-5"} if kind == "anthropic" else {"main": "gpt-main", "cheap": "gpt-cheap"})
    cls = AnthropicProvider if kind == "anthropic" else OpenAIProvider
    return cls(api_key=KEY, models=models, http_client=http, max_retries=0, **kw)


def test_anthropic_request_shape_and_usage(sf):
    seen = []

    def handler(request):
        seen.append((request, json.loads(request.content)))
        return httpx2.Response(200, json=_anthropic_body(json.dumps(GOOD)))

    r = client(_provider("anthropic", handler), sf).complete(**CALL)
    request, body = seen[0]
    assert request.url.path == "/v1/messages" and request.headers["authorization"] == f"Bearer {KEY}"
    assert body["model"] == "claude-opus-5-5" and body["max_tokens"] == 500
    assert body["system"] == [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]
    assert body["messages"] == [{"role": "user", "content": "USER"}]
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "medium"}
    assert not {"thinking", "temperature", "top_p", "top_k"} & body.keys()
    assert r.data == GOOD and r.usage == Usage(1000, 200, 300, 400)
    assert r.cost_usd == estimate_cost("anthropic", "claude-opus-5-5", r.usage)
    (row,) = rows(sf)
    assert row.provider == "anthropic" and row.cache_read_tokens == 400


def test_anthropic_cheap_tier_uses_default_haiku_and_low_effort():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json=_anthropic_body(json.dumps(GOOD)))

    p = _provider("anthropic", handler, models={"main": "", "cheap": "change-me-cheap-model"})
    assert p.model_for("main") == "claude-opus-5-5" and p.model_for("cheap") == "claude-haiku-5-5"
    p.complete_json(purpose="x", tier="cheap", system="s", user="u", schema=SCHEMA, max_tokens=10)
    assert seen[0]["model"] == "claude-haiku-5-5" and seen[0]["output_config"]["effort"] == "low"


def test_anthropic_schema_mismatch_retries_once(sf):
    answers = iter([_anthropic_body("nope"), _anthropic_body(json.dumps(GOOD))])
    calls = []

    def handler(request):
        calls.append(1)
        return httpx2.Response(200, json=next(answers))

    r = client(_provider("anthropic", handler), sf).complete(**CALL)
    assert r.data == GOOD and len(calls) == 2 and r.usage.input_tokens == 2000

    calls.clear()
    with pytest.raises(LLMError, match="schema_mismatch"):
        client(_provider("anthropic", lambda req: (calls.append(1), httpx2.Response(200, json=_anthropic_body("{}")))[1]), sf).complete(**{**CALL, "user": "z"})
    assert len(calls) == 2


@pytest.mark.parametrize("stop,code", [("refusal", "refusal"), ("max_tokens", "max_tokens")])
def test_anthropic_refusal_and_max_tokens_fail_with_usage(sf, stop, code):
    body = _anthropic_body("", stop=stop)
    if stop == "refusal":
        body["stop_details"] = {"type": "refusal", "category": "general_harms", "explanation": "SECRET-EXPLANATION"}
    with pytest.raises(LLMError) as ei:
        client(_provider("anthropic", lambda req: httpx2.Response(200, json=body)), sf).complete(**CALL)
    assert ei.value.code == code
    (row,) = rows(sf)
    assert (row.status, row.error, row.input_tokens) == ("failed", code, 1000)
    assert "SECRET-EXPLANATION" not in json.dumps([row.error])


@pytest.mark.parametrize("status,code", [(429, "rate_limited"), (500, "http_500"), (401, "http_401"), (400, "http_400"), (403, "http_403"), (404, "http_404"), (529, "http_529")])
@pytest.mark.parametrize("kind", ["anthropic", "openai"])
def test_http_errors_map_to_fixed_codes(sf, kind, status, code):
    secret_echo = "echoed prompt USER and key " + KEY
    body = {"type": "error", "error": {"type": "x", "message": secret_echo}} if kind == "anthropic" else {"error": {"message": secret_echo, "type": "x"}}
    with pytest.raises(LLMError) as ei:
        client(_provider(kind, lambda req: httpx2.Response(status, json=body)), sf).complete(**CALL)
    assert ei.value.code == code and ei.value.__cause__ is None and ei.value.__suppress_context__
    (row,) = rows(sf)
    assert (row.status, row.error) == ("failed", code)


@pytest.mark.parametrize("kind", ["anthropic", "openai"])
def test_connection_error(sf, kind):
    def handler(request):
        raise httpx2.ConnectError("boom " + KEY, request=request)

    with pytest.raises(LLMError, match="connection_error"):
        client(_provider(kind, handler), sf).complete(**CALL)


# --- OpenAI -----------------------------------------------------------------------------------------

def _openai_body(content, *, finish="stop", refusal=None, usage=None):
    return {
        "id": "c1", "object": "chat.completion", "created": 1, "model": "gpt-main",
        "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content, "refusal": refusal}}],
        "usage": usage or {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200, "prompt_tokens_details": {"cached_tokens": 400}},
    }


def test_openai_request_shape_usage_and_unknown_model_price(sf, caplog):
    pricing._warned.clear()
    seen = []

    def handler(request):
        seen.append((request, json.loads(request.content)))
        return httpx2.Response(200, json=_openai_body(json.dumps(GOOD)))

    p = _provider("openai", handler, base_url="https://llm.example.com/v1")
    with caplog.at_level(logging.WARNING):
        r = client(p, sf).complete(**CALL)
    request, body = seen[0]
    assert str(request.url) == "https://llm.example.com/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert body["model"] == "gpt-main" and body["max_completion_tokens"] == 500
    assert body["messages"] == [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}]
    assert body["response_format"] == {"type": "json_schema", "json_schema": {"name": "result", "schema": SCHEMA, "strict": True}}
    assert r.usage == Usage(input_tokens=600, output_tokens=200, cache_read_tokens=400)
    assert r.cost_usd == estimate_cost("openai", "gpt-main", r.usage) > 0
    assert any("gpt-main" in rec.getMessage() for rec in caplog.records)


@pytest.mark.parametrize(
    "body,code",
    [
        (_openai_body(None, refusal="I cannot help with that"), "refusal"),
        (_openai_body("", finish="content_filter"), "refusal"),
        (_openai_body('{"summary":', finish="length"), "max_tokens"),
    ],
)
def test_openai_refusal_and_length(sf, body, code):
    with pytest.raises(LLMError) as ei:
        client(_provider("openai", lambda req: httpx2.Response(200, json=body)), sf).complete(**CALL)
    assert ei.value.code == code


def test_openai_schema_mismatch_retry(sf):
    answers = iter([_openai_body("[]"), _openai_body(json.dumps(GOOD))])
    r = client(_provider("openai", lambda req: httpx2.Response(200, json=next(answers))), sf).complete(**CALL)
    assert r.data == GOOD


def test_openai_missing_model_is_config_error(sf):
    called = []
    p = _provider("openai", lambda req: called.append(1), models={"main": "change-me-main-model", "cheap": ""})
    for tier in ("main", "cheap"):
        with pytest.raises(LLMError) as ei:
            client(p, sf).complete(**{**CALL, "tier": tier})
        assert ei.value.code == "config_error"
    assert not called and rows(sf) == []


def test_missing_api_key_is_config_error(sf):
    for cls in (AnthropicProvider, OpenAIProvider):
        p = cls(api_key="change-me-llm-api-key", models={"main": "m", "cheap": "m"})
        with pytest.raises(LLMError, match="config_error"):
            client(p, sf).complete(**CALL)
    assert rows(sf) == []


# --- factory ----------------------------------------------------------------------------------------

def _settings(monkeypatch, **env):
    for k in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL_CHEAP", "LLM_MODEL_MAIN", "LLM_API_BASE", "LLM_DAILY_BUDGET_USD", "LLM_MAX_OUTPUT_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings()


def test_factory(monkeypatch, sf):
    s = _settings(monkeypatch)
    assert s.llm_provider == "none" and s.llm_daily_budget_usd == Decimal("1.00") and s.llm_max_output_tokens == 16000
    assert build_provider(s) is None and llm.build_client(s, sf) is None
    s = _settings(monkeypatch, LLM_PROVIDER="anthropic", LLM_API_KEY=KEY, LLM_MODEL_MAIN="change-me-main-model")
    p = build_provider(s)
    assert isinstance(p, AnthropicProvider) and p.model_for("main") == "claude-opus-5-5"
    assert KEY not in repr(s) and s.llm_api_key_is_set
    s = _settings(monkeypatch, LLM_PROVIDER="OpenAI", LLM_API_KEY=KEY, LLM_API_BASE="https://x.example/v1", LLM_MODEL_MAIN="gpt-main", LLM_DAILY_BUDGET_USD="0")
    p = build_provider(s)
    assert isinstance(p, OpenAIProvider) and p.model_for("main") == "gpt-main" and p.model_for("cheap") is None
    assert llm.build_client(s, sf).daily_budget_usd == 0
    with pytest.raises(LLMError, match="config_error"):
        build_provider(_settings(monkeypatch, LLM_PROVIDER="bogus"))


# --- R11: the key never shows up ---------------------------------------------------------------------

def test_api_key_never_in_logs_or_database(sf, caplog):
    def handler(request):
        if b"fail" in request.content:
            return httpx2.Response(401, json={"error": {"message": "bad key " + KEY, "type": "authentication_error"}})
        return httpx2.Response(200, json=_anthropic_body(json.dumps(GOOD)))

    p = _provider("anthropic", handler)
    c = client(p, sf)
    with caplog.at_level(logging.DEBUG):
        c.complete(**CALL)
        c.complete(**CALL)
        with pytest.raises(LLMError):
            c.complete(**{**CALL, "user": "fail"})
        with pytest.raises(LLMError):
            c.complete(**{**CALL, "user": "fail", "tier": "cheap"})
    assert KEY not in caplog.text and KEY not in repr(p.__dict__.get("_models"))
    with get_engine().connect() as conn:
        tables = [r[0] for r in conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))]
        for t in tables:
            dump = "\n".join(r[0] for r in conn.execute(text(f'SELECT x::text FROM "{t}" x')))
            assert KEY not in dump, t


# --- address masking ---------------------------------------------------------------------------------

def test_mask_addresses():
    src = "Alice <alice@example.com> wrote; cc bob.smith+list@mail.example.org, <CAF=ab12-x@mail.example.net> and a@x.example.com."
    out = mask_addresses(src)
    assert "@" not in out
    assert out == "Alice <[address]> wrote; cc [address], <[address]> and [address]."
    assert mask_addresses("no address here: 100@ 5, @mention, x@y") == "no address here: 100@ 5, @mention, x@y"
    assert mask_addresses("山田 <taro@例え.example.com>") == "山田 <[address]>"
