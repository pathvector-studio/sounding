"""The one place LLM calls go through: reuse (R7), daily budget (R8), call log (R7).

``LLMClient.complete`` does, in order:
1. resolve the model for the tier (unset -> ``config_error``, nothing logged: no call was made);
2. look for an ``ok`` row with the same ``request_hash`` -> return its response, log a ``reused``
   row (cost 0) so avoided charges stay visible;
3. check today's spend (TZ calendar day) against the budget -> ``budget_exceeded``, no call, no row
   (the caller records the skipped conversation in job_runs);
4. call the provider; log an ``ok`` or ``failed`` row (a failed call may still have cost tokens).

DB work uses short transactions of its own and never spans the HTTP call, and the log row is
committed even if the caller's transaction rolls back later.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, time as dtime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sounding.config import Settings
from sounding.llm.base import LLMError, LLMResult, Tier, Usage
from sounding.llm.pricing import estimate_cost
from sounding.llm.providers import LLMProvider, build_provider
from sounding.models import LlmCall

log = logging.getLogger(__name__)


def request_hash(
    *, provider: str, model: str, tier: str, system: str, user: str, schema: dict[str, Any],
    prompt_version: str, language: str,
) -> str:
    payload = json.dumps(
        {
            "provider": provider, "model": model, "tier": tier, "system": system, "user": user,
            "schema": schema, "prompt_version": prompt_version, "language": language,
        },
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def day_bounds(tz: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    """[start, end) of the calendar day in ``tz`` that contains ``now`` (aware datetimes)."""
    zone = ZoneInfo(tz)
    local = (now or datetime.now(UTC)).astimezone(zone)
    start = datetime.combine(local.date(), dtime.min, tzinfo=zone)
    end = datetime.combine(local.date() + timedelta(days=1), dtime.min, tzinfo=zone)
    return start, end


def spend_today(session: Session, tz: str, now: datetime | None = None) -> Decimal:
    start, end = day_bounds(tz, now)
    total = session.scalar(
        select(func.coalesce(func.sum(LlmCall.cost_usd), 0)).where(
            LlmCall.created_at >= start, LlmCall.created_at < end
        )
    )
    return Decimal(total or 0)


def calls_today(session: Session, tz: str, now: datetime | None = None) -> dict[str, int]:
    start, end = day_bounds(tz, now)
    rows = session.execute(
        select(LlmCall.status, func.count()).where(LlmCall.created_at >= start, LlmCall.created_at < end).group_by(LlmCall.status)
    ).all()
    return {status: n for status, n in rows}


class LLMClient:
    def __init__(
        self,
        provider: LLMProvider,
        session_factory: Callable[[], Session],
        *,
        tz: str,
        daily_budget_usd: Decimal,
        default_max_tokens: int = 16000,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.provider = provider
        self._sf = session_factory
        self.tz = tz
        self.daily_budget_usd = daily_budget_usd
        self.default_max_tokens = default_max_tokens
        self._clock = clock

    def _now(self) -> datetime | None:
        return self._clock() if self._clock else None

    def complete(
        self,
        *,
        purpose: str,
        tier: Tier,
        system: str,
        user: str,
        schema: dict[str, Any],
        prompt_version: str,
        language: str,
        max_tokens: int | None = None,
        conversation_id: int | None = None,
    ) -> LLMResult:
        model = self.provider.model_for(tier)
        if model is None:
            raise LLMError("config_error")
        key = request_hash(
            provider=self.provider.name, model=model, tier=tier, system=system, user=user,
            schema=schema, prompt_version=prompt_version, language=language,
        )

        with self._sf() as s:
            earlier = s.scalar(
                select(LlmCall).where(LlmCall.request_hash == key, LlmCall.status == "ok").order_by(LlmCall.id.desc()).limit(1)
            )
            if earlier is not None and earlier.response_json is not None:
                row = LlmCall(
                    purpose=purpose, provider=self.provider.name, model=model, tier=tier, request_hash=key,
                    status="reused", cost_usd=Decimal(0), duration_ms=0, conversation_id=conversation_id,
                )
                s.add(row)
                s.commit()
                return LLMResult(
                    data=earlier.response_json, provider=self.provider.name, model=model, tier=tier,
                    usage=Usage(), cost_usd=Decimal(0), duration_ms=0, reused=True,
                    request_hash=key, call_id=row.id,
                )
            if self.daily_budget_usd > 0 and spend_today(s, self.tz, self._now()) >= self.daily_budget_usd:
                raise LLMError("budget_exceeded")

        started = time.monotonic()
        try:
            result = self.provider.complete_json(
                purpose=purpose, tier=tier, system=system, user=user, schema=schema,
                max_tokens=max_tokens or self.default_max_tokens,
            )
        except LLMError as exc:
            if exc.code != "config_error":  # config_error: nothing was sent, nothing to log
                self._log(
                    purpose=purpose, model=model, tier=tier, key=key, status="failed", error=exc.code,
                    usage=exc.usage, cost=estimate_cost(self.provider.name, model, exc.usage),
                    duration_ms=int((time.monotonic() - started) * 1000), conversation_id=conversation_id,
                )
            raise
        result.request_hash = key
        result.call_id = self._log(
            purpose=purpose, model=result.model, tier=tier, key=key, status="ok", error=None,
            usage=result.usage, cost=result.cost_usd, duration_ms=result.duration_ms,
            conversation_id=conversation_id, response=result.data,
        )
        return result

    def _log(
        self, *, purpose: str, model: str, tier: str, key: str, status: str, error: str | None,
        usage: Usage, cost: Decimal, duration_ms: int, conversation_id: int | None, response: Any = None,
    ) -> int:
        with self._sf() as s:
            row = LlmCall(
                purpose=purpose, provider=self.provider.name, model=model, tier=tier, request_hash=key,
                status=status, error=error, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                cache_write_tokens=usage.cache_write_tokens, cache_read_tokens=usage.cache_read_tokens,
                cost_usd=cost, duration_ms=duration_ms, response_json=response, conversation_id=conversation_id,
            )
            s.add(row)
            s.commit()
            return row.id


def build_client(settings: Settings, session_factory: Callable[[], Session], *, provider: LLMProvider | None = None) -> LLMClient | None:
    """An LLMClient for the configured provider, or None when ``LLM_PROVIDER=none`` (R1: callers check)."""
    provider = provider or build_provider(settings)
    if provider is None:
        return None
    return LLMClient(
        provider, session_factory, tz=settings.tz, daily_budget_usd=settings.llm_daily_budget_usd,
        default_max_tokens=settings.llm_max_output_tokens,
    )
