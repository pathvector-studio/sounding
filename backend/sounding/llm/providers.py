"""Providers: Anthropic and OpenAI (official SDKs), and a fake one for tests.

Callers pass a tier (cheap/main); the model name comes from settings. Each provider enforces the
JSON Schema on the server (structured output) and checks it again here; an invalid answer is
retried once, then fails with ``schema_mismatch``. Failures carry fixed codes only; SDK exception
messages are dropped (``from None``) because they can echo request content.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from sounding.config import Settings, is_set
from sounding.llm.base import (
    LLMError,
    LLMResult,
    RawResponse,
    SchemaError,
    Tier,
    Usage,
    _Request,
    parse_and_validate,
)
from sounding.llm.pricing import estimate_cost

log = logging.getLogger(__name__)

# Depth of the model's reasoning is set explicitly (claude-opus-5-5 always thinks; `thinking` and
# sampling parameters must not be sent).
ANTHROPIC_EFFORT: dict[str, str] = {"main": "medium", "cheap": "low"}
ANTHROPIC_DEFAULT_MODELS: dict[str, str] = {"main": "claude-opus-5-5", "cheap": "claude-haiku-5-5"}

REQUEST_TIMEOUT_SECONDS = 300.0  # a long conversation plus reasoning can take minutes
SDK_MAX_RETRIES = 2  # the SDK's own retries for 429 / 5xx / connection errors
ATTEMPTS = 2  # one try plus one retry when the answer does not match the schema


class LLMProvider(Protocol):
    name: str

    def model_for(self, tier: Tier) -> str | None: ...

    def complete_json(
        self, *, purpose: str, tier: Tier, system: str, user: str, schema: dict[str, Any], max_tokens: int
    ) -> LLMResult: ...


class BaseProvider:
    name = "base"

    def __init__(self, models: dict[str, str | None]) -> None:
        self._models = models

    def model_for(self, tier: Tier) -> str | None:
        value = self._models.get(tier)
        return value if is_set(value) else None

    def _call_once(
        self, *, model: str, tier: Tier, system: str, user: str, schema: dict[str, Any], max_tokens: int
    ) -> RawResponse:
        raise NotImplementedError

    def complete_json(
        self, *, purpose: str, tier: Tier, system: str, user: str, schema: dict[str, Any], max_tokens: int
    ) -> LLMResult:
        model = self.model_for(tier)
        if model is None:
            raise LLMError("config_error")
        started = time.monotonic()
        total = Usage()
        for attempt in range(1, ATTEMPTS + 1):
            try:
                raw = self._call_once(
                    model=model, tier=tier, system=system, user=user, schema=schema, max_tokens=max_tokens
                )
            except LLMError as exc:
                exc.usage = total + exc.usage
                raise
            total = total + raw.usage
            try:
                data = parse_and_validate(raw.text, schema)
            except SchemaError as exc:
                log.warning("llm %s answer rejected (attempt %d/%d): %s", purpose, attempt, ATTEMPTS, exc)
                if attempt == ATTEMPTS:
                    raise LLMError("schema_mismatch", usage=total) from None
                continue
            return LLMResult(
                data=data,
                provider=self.name,
                model=model,
                tier=tier,
                usage=total,
                cost_usd=estimate_cost(self.name, model, total),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        raise AssertionError("unreachable")  # pragma: no cover


def _status_code(status: int) -> str:
    return "rate_limited" if status == 429 else f"http_{status}"


class AnthropicProvider(BaseProvider):
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        models: dict[str, str | None],
        http_client: Any = None,
        max_retries: int = SDK_MAX_RETRIES,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__({**ANTHROPIC_DEFAULT_MODELS, **{k: v for k, v in models.items() if is_set(v)}})
        self._api_key = api_key
        self._http_client = http_client
        self._max_retries = max_retries
        self._timeout = timeout
        self._client: Any = None

    def _sdk(self) -> Any:
        if self._client is None:
            if not is_set(self._api_key):
                raise LLMError("config_error")
            import anthropic

            self._client = anthropic.Anthropic(
                api_key=self._api_key,
                timeout=self._timeout,
                max_retries=self._max_retries,
                http_client=self._http_client,
            )
        return self._client

    def _call_once(self, *, model, tier, system, user, schema, max_tokens) -> RawResponse:  # type: ignore[override]
        import anthropic

        client = self._sdk()
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config={
                    "format": {"type": "json_schema", "schema": schema},
                    "effort": ANTHROPIC_EFFORT[tier],
                },
            )
        except anthropic.APIStatusError as exc:
            raise LLMError(_status_code(exc.status_code)) from None
        except anthropic.APIError:  # connection, timeout, anything else from the SDK
            raise LLMError("connection_error") from None
        u = response.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_write_tokens=u.cache_creation_input_tokens or 0,
            cache_read_tokens=u.cache_read_input_tokens or 0,
        )
        if response.stop_reason == "refusal":
            raise LLMError("refusal", usage=usage)
        if response.stop_reason == "max_tokens":
            raise LLMError("max_tokens", usage=usage)
        text = next((b.text for b in response.content if b.type == "text"), "")
        return RawResponse(text, usage)


class OpenAIProvider(BaseProvider):
    """Chat Completions with ``response_format`` json_schema (strict).

    Chosen over the Responses API because Chat Completions is what OpenAI-compatible servers
    (``LLM_API_BASE``: Ollama, vLLM, gateways) implement, and its usage and refusal fields are
    simple. ``max_completion_tokens`` is used because reasoning models reject ``max_tokens``.
    """

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        models: dict[str, str | None],
        base_url: str | None = None,
        http_client: Any = None,
        max_retries: int = SDK_MAX_RETRIES,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(dict(models))  # no default models: unset is a configuration error
        self._api_key = api_key
        self._base_url = base_url or None
        self._http_client = http_client
        self._max_retries = max_retries
        self._timeout = timeout
        self._client: Any = None

    def _sdk(self) -> Any:
        if self._client is None:
            if not is_set(self._api_key):
                raise LLMError("config_error")
            import openai

            self._client = openai.OpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._timeout,
                max_retries=self._max_retries,
                http_client=self._http_client,
            )
        return self._client

    def _call_once(self, *, model, tier, system, user, schema, max_tokens) -> RawResponse:  # type: ignore[override]
        import openai

        client = self._sdk()
        try:
            response = client.chat.completions.create(
                model=model,
                max_completion_tokens=max_tokens,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "result", "schema": schema, "strict": True},
                },
            )
        except openai.APIStatusError as exc:
            raise LLMError(_status_code(exc.status_code)) from None
        except openai.APIError:
            raise LLMError("connection_error") from None
        usage = Usage()
        if response.usage is not None:
            details = response.usage.prompt_tokens_details
            cached = (details.cached_tokens or 0) if details is not None else 0
            usage = Usage(
                input_tokens=max(response.usage.prompt_tokens - cached, 0),
                output_tokens=response.usage.completion_tokens,
                cache_read_tokens=cached,
            )
        if not response.choices:
            raise LLMError("schema_mismatch", usage=usage)
        choice = response.choices[0]
        if choice.message.refusal or choice.finish_reason == "content_filter":
            raise LLMError("refusal", usage=usage)
        if choice.finish_reason == "length":
            raise LLMError("max_tokens", usage=usage)
        return RawResponse(choice.message.content or "", usage)


FakeScript = str | dict | list | RawResponse | Exception | Callable[[_Request], Any]


class FakeProvider(BaseProvider):
    """For tests: answers from a script, records every request, never touches the network.

    Each script item is one answer to ``_call_once``: a ``str`` (raw text), a dict/list (sent as
    JSON), a ``RawResponse``, an exception to raise, or a callable taking the request. The last
    item repeats when the script runs out. Retry and schema checking are the real ones.
    """

    name = "fake"

    def __init__(
        self,
        script: list[FakeScript] | FakeScript,
        *,
        models: dict[str, str | None] | None = None,
        usage: Usage = Usage(input_tokens=1000, output_tokens=200),
    ) -> None:
        super().__init__(models or {"cheap": "claude-haiku-5-5", "main": "claude-opus-5-5"})
        self.script: list[FakeScript] = list(script) if isinstance(script, list) else [script]
        self.default_usage = usage
        self.requests: list[_Request] = []

    def _call_once(self, *, model, tier, system, user, schema, max_tokens) -> RawResponse:  # type: ignore[override]
        req = _Request(getattr(self, "_purpose", ""), tier, model, system, user, schema, max_tokens)
        self.requests.append(req)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if callable(item) and not isinstance(item, (str, dict, list)):
            item = item(req)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, RawResponse):
            return item
        text = item if isinstance(item, str) else json.dumps(item)
        return RawResponse(text, self.default_usage)

    def complete_json(self, *, purpose, **kw):  # type: ignore[override]
        self._purpose = purpose
        return super().complete_json(purpose=purpose, **kw)

    @property
    def calls(self) -> int:
        return len(self.requests)


def build_provider(settings: Settings, *, http_client: Any = None, max_retries: int = SDK_MAX_RETRIES) -> LLMProvider | None:
    """The provider for ``LLM_PROVIDER``; None when it is ``none`` (nothing may be sent out).

    A missing key or model is not an error here: the call fails with ``config_error`` so a worker
    keeps running and the job records why.
    """
    kind = settings.llm_provider.strip().lower()
    models = {"cheap": settings.llm_model_cheap, "main": settings.llm_model_main}
    key = settings.llm_api_key.get_secret_value()
    if kind in ("", "none"):
        return None
    if kind == "anthropic":
        return AnthropicProvider(api_key=key, models=models, http_client=http_client, max_retries=max_retries)
    if kind == "openai":
        return OpenAIProvider(
            api_key=key, models=models, base_url=settings.llm_api_base.strip() or None,
            http_client=http_client, max_retries=max_retries,
        )
    raise LLMError("config_error")
