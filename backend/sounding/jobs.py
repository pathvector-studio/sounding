"""Jobs. ``run_fetch`` pulls every enabled source once and writes one ``job_runs`` row per source.

One source failing never stops the others. Error text stored in the database is a fixed token or
an exception type name, never server output (it could carry secrets or addresses).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.orm import Session

from sounding.ancestors import complete_ancestors, merge_missing
from sounding.config import Settings
from sounding.ingest import ingest_items
from sounding.llm import LLMClient, LLMError, build_client
from sounding.models import Item, JobRun, LlmCall, Source
from sounding.profile import Profile, load_profile, sync_sources
from sounding.sources.base import AncestorFetcher, SourceAdapter, SourceError

log = logging.getLogger(__name__)

WARN_AFTER_FAILURES = 3
_KIND_ORDER = {"ietf_mail": 0, "rss": 1}

SessionFactory = Callable[[], Session]


@dataclass
class SourceOutcome:
    kind: str
    key: str
    status: str  # ok | failed | skipped
    counts: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def default_adapters(settings: Settings) -> dict[str, SourceAdapter]:
    from sounding.sources.ietf_mail import IetfMailAdapter
    from sounding.sources.rss import RssAdapter

    return {"ietf_mail": IetfMailAdapter(settings), "rss": RssAdapter(settings)}


def _finish(
    session: Session,
    source: Source,
    *,
    started: datetime,
    status: str,
    last_status: str,
    counts: dict[str, Any],
    error: str | None,
    cursor: dict[str, Any] | None = None,
    touch_cursor: bool = False,
) -> SourceOutcome:
    if status == "ok":
        source.consecutive_failures = 0
        source.last_fetched_at = datetime.now(UTC)
    elif status == "failed":
        source.consecutive_failures = (source.consecutive_failures or 0) + 1
        if source.consecutive_failures >= WARN_AFTER_FAILURES:
            counts = {**counts, "warning": "consecutive_failures", "consecutive_failures": source.consecutive_failures}
            log.warning("source %s %s has failed %d times in a row", source.kind, source.key, source.consecutive_failures)
    source.last_status = last_status
    source.last_error = error
    if touch_cursor:
        source.cursor = cursor
    session.add(
        JobRun(
            job="fetch", source_id=source.id, started_at=started, finished_at=datetime.now(UTC),
            status=status, counts=counts or None, error=error,
        )
    )
    session.commit()
    return SourceOutcome(source.kind, source.key, status, counts, error)


def _fetch_one(
    session_factory: SessionFactory, settings: Settings, adapter: SourceAdapter, source_id: int, now: datetime
) -> SourceOutcome:
    started = datetime.now(UTC)
    with session_factory() as session:
        source = session.get(Source, source_id)
        assert source is not None
        if source.kind == "ietf_mail":
            # Without these, mail must not be stored (no contact for IETF; no author_key without the secret).
            reason = (
                "fetch_contact_not_set" if not settings.fetch_contact_is_set
                else "identity_secret_not_set" if not settings.identity_secret_is_set
                else None
            )
            if reason:
                return _finish(
                    session, source, started=started, status="skipped", last_status="skipped",
                    counts={"reason": reason}, error=reason,
                )
        cursor = dict(source.cursor) if source.cursor else None
        try:
            result = adapter.fetch(source, cursor)
            if result.not_found:
                return _finish(
                    session, source, started=started, status="failed", last_status="not_found",
                    counts={"notes": list(result.notes)}, error="mailbox_not_found",
                )
            ing = ingest_items(session, source, result.items, fetched_via="poll")
            counts: dict[str, Any] = {
                "fetched": len(result.items), "new": len(ing.new), "duplicates": ing.duplicates, "events": ing.events,
            }
            new_cursor = dict(result.cursor) if result.cursor is not None else None
            if result.notes:
                counts["notes"] = list(result.notes)
            if not result.error and new_cursor is not None and isinstance(adapter, AncestorFetcher):
                missing = list(new_cursor.get("missing_ids") or [])
                anc = complete_ancestors(
                    session, source, adapter, result.items, missing_ids=missing,
                    max_depth=settings.ietf_ancestor_max_depth,
                    max_age_days=settings.ietf_ancestor_max_age_days, now=now,
                )
                new_cursor["missing_ids"] = merge_missing(missing, anc.missing_added)
                counts.update(
                    ancestors=anc.ingested, ancestor_lookups=anc.looked_up, ancestors_skipped_old=anc.skipped_old,
                    missing_ids_added=len(anc.missing_added), events=ing.events + anc.events,
                )
                if anc.error:
                    counts["ancestor_error"] = anc.error
            if result.error:
                counts["stopped_early"] = result.error
            return _finish(
                session, source, started=started, status="failed" if result.error else "ok",
                last_status="failed" if result.error else "ok", counts=counts, error=result.error,
                cursor=new_cursor, touch_cursor=new_cursor is not None,
            )
        except Exception as exc:  # noqa: BLE001 - isolate the source; details by type only
            session.rollback()
            code = exc.code if isinstance(exc, SourceError) else type(exc).__name__
            log.warning("fetch failed for %s %s: %s", source.kind, source.key, code)
            source = session.get(Source, source_id)
            assert source is not None
            return _finish(
                session, source, started=started, status="failed", last_status="failed", counts={}, error=code
            )


def run_fetch(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    adapters: Mapping[str, SourceAdapter] | None = None,
    only_source: str | None = None,
    profile: Profile | None = None,
    now: datetime | None = None,
    stop: threading.Event | None = None,
) -> list[SourceOutcome]:
    """Sync the profile into ``sources``, then fetch each enabled source with a registered adapter.

    ``stop`` is checked between sources: once set, the remaining sources are left for the next run."""
    profile = profile if profile is not None else load_profile(settings.profile_path)
    owned = adapters is None
    registry = dict(default_adapters(settings) if adapters is None else adapters)
    with session_factory() as session:
        sources = sync_sources(session, profile)
        session.commit()
        todo = [
            (s.id, s.kind) for s in sources
            if s.enabled and s.kind in registry and (only_source is None or s.key == only_source)
        ]
    todo.sort(key=lambda t: _KIND_ORDER.get(t[1], 99))
    outcomes: list[SourceOutcome] = []
    try:
        for source_id, kind in todo:
            if stop is not None and stop.is_set():
                log.info("stop requested; leaving %d source(s) for the next run", len(todo) - len(outcomes))
                break
            outcomes.append(_fetch_one(session_factory, settings, registry[kind], source_id, now or datetime.now(UTC)))
    finally:
        if owned:
            for adapter in registry.values():
                close = getattr(adapter, "close", None)
                if callable(close):
                    close()
    return outcomes


def run_retention(
    session_factory: SessionFactory, settings: Settings, now: datetime | None = None
) -> dict[str, int]:
    """Clear ``body_text`` of items fetched more than CONTENT_RETENTION_DAYS ago (0 disables).

    Metadata and ``body_hash`` stay. Writes one ``job_runs`` row (job='retention')."""
    started = datetime.now(UTC)
    now = now or started
    days = settings.content_retention_days
    with session_factory() as session:
        if days <= 0:
            counts: dict[str, Any] = {"cleared": 0, "disabled": True}
        else:
            cutoff = now - timedelta(days=days)
            result = session.execute(
                update(Item).where(Item.body_text.is_not(None), Item.fetched_at < cutoff).values(body_text=None)
            )
            counts = {"cleared": result.rowcount}
        session.add(
            JobRun(job="retention", started_at=started, finished_at=datetime.now(UTC), status="ok", counts=counts)
        )
        session.commit()
    return counts


BUDGET_SKIPPED_IDS_CAP = 300


@dataclass
class SummarizeReport:
    status: str  # ok | failed | skipped
    reason: str | None = None  # llm_disabled | config_error | all_failed | None
    counts: dict[str, Any] = field(default_factory=dict)
    outcomes: list[Any] = field(default_factory=list)  # state.Outcome, in processing order
    budget_skipped_ids: list[int] = field(default_factory=list)


def _record_summarize(
    session_factory: SessionFactory, started: datetime, status: str, counts: dict[str, Any], error: str | None
) -> None:
    with session_factory() as session:
        session.add(
            JobRun(
                job="summarize", started_at=started, finished_at=datetime.now(UTC), status=status,
                counts=counts or None, error=error,
            )
        )
        session.commit()


def run_summarize(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    client: LLMClient | None = None,
    profile: Profile | None = None,
    stop: threading.Event | None = None,
    only_conversation: int | None = None,
    now: datetime | None = None,
) -> SummarizeReport:
    """Create the next "where it stands" version of every conversation that has new human messages.

    Writes one ``job_runs`` row (job='summarize', source_id NULL):
    * ``LLM_PROVIDER=none`` -> ``skipped`` / ``llm_disabled``; no provider is built, nothing is sent (R1).
    * the first ``budget_exceeded`` stops LLM calls for the rest of the run; the conversations left are
      listed in ``counts.budget_skipped_ids`` (capped at 300; ``budget_skipped`` is the full count) (R8).
      The run's status stays ``ok``: stopping at the budget is the designed behaviour.
    * ``config_error`` (missing key/model; it would fail for every conversation) stops the run -> ``failed``.
    * other failures are counted per code in ``counts.failed`` and the run goes on. The status is ``ok``
      unless every selected conversation failed (``failed`` / ``all_failed``).
    * ``stop`` is checked between conversations; the rest is left for the next run (``counts.stopped``).
    Logs carry conversation ids and statuses only. Error text in the database is a fixed token.
    """
    from sounding.state import conversations_to_summarize, summarize_conversation

    started = datetime.now(UTC)
    client = client if client is not None else build_client(settings, session_factory)
    if client is None:
        log.info("summarize skipped: LLM_PROVIDER is none")
        _record_summarize(session_factory, started, "skipped", {"reason": "llm_disabled"}, "llm_disabled")
        return SummarizeReport("skipped", "llm_disabled", {"reason": "llm_disabled"})

    profile = profile if profile is not None else load_profile(settings.profile_path)
    language = profile.profile.language or "en"
    if only_conversation is not None:
        ids = [only_conversation]
    else:
        with session_factory() as session:
            ids = conversations_to_summarize(session, profile, language=language)

    outcomes: list[Any] = []
    failed: dict[str, int] = {}
    budget_skipped: list[int] = []
    call_ids: list[int] = []
    created = updated = rebuilt = no_new = skipped = downgraded = 0
    stopped = 0
    config_error = False
    budget_hit = False
    for pos, cid in enumerate(ids):
        if stop is not None and stop.is_set():
            stopped = len(ids) - pos
            log.info("stop requested; leaving %d conversation(s) for the next run", stopped)
            break
        if budget_hit:
            budget_skipped.append(cid)
            continue
        out = summarize_conversation(session_factory, client, cid, language=language, now=now)
        outcomes.append(out)
        call_ids.extend(out.llm_call_ids)
        downgraded += out.downgraded
        log.info("summarize conversation %d: %s%s", cid, out.status, f" ({out.code})" if out.code else "")
        if out.status == "failed":
            code = out.code or "unknown"
            if code == "budget_exceeded":
                budget_hit = True
                budget_skipped.append(cid)
                continue
            failed[code] = failed.get(code, 0) + 1
            if code == "config_error":
                config_error = True
                stopped = len(ids) - pos - 1
                break
        elif out.status == "created":
            created += 1
        elif out.status == "updated":
            updated += 1
        elif out.status == "rebuilt":
            rebuilt += 1
        elif out.status == "no_new":
            no_new += 1
        else:
            skipped += 1

    cost = Decimal(0)
    if call_ids:
        with session_factory() as session:
            cost = Decimal(session.scalar(select(func.coalesce(func.sum(LlmCall.cost_usd), 0)).where(LlmCall.id.in_(call_ids))) or 0)
    n_failed = sum(failed.values())
    counts: dict[str, Any] = {
        "selected": len(ids), "created": created, "updated": updated, "rebuilt": rebuilt, "no_new": no_new,
        "skipped": skipped, "failed": failed, "downgraded": downgraded, "llm_calls": len(call_ids),
        "cost_usd": f"{cost:.6f}", "budget_skipped": len(budget_skipped),
    }
    if budget_skipped:
        counts["budget_skipped_ids"] = budget_skipped[:BUDGET_SKIPPED_IDS_CAP]
        counts["budget_skipped_note"] = "not summarized: daily budget exceeded"
    if stopped:
        counts["stopped"] = stopped
    reason: str | None = None
    status = "ok"
    if config_error:
        status, reason = "failed", "config_error"
    elif n_failed and n_failed == len(ids):
        status, reason = "failed", "all_failed"
    _record_summarize(session_factory, started, status, counts, reason)
    return SummarizeReport(status, reason, counts, outcomes, budget_skipped)


@dataclass
class DigestReport:
    status: str  # ok | failed
    reason: str | None = None  # config_error | None
    digest_id: int | None = None
    rebuilt: bool = False
    counts: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    language: str = "en"
    delivery: str | None = None


NO_CHANNELS = "no channels implemented yet"


def deliver_digest(session_factory: SessionFactory, settings: Settings, digest_id: int) -> str:
    """Send a built digest to the enabled channels. Phase 3 item 4 fills this in (email, webhook and the
    ``deliveries`` records); until then it sends nothing and says so."""
    log.debug("digest %d: delivery skipped: %s", digest_id, NO_CHANNELS)
    return NO_CHANNELS


def run_digest(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    client: LLMClient | None = None,
    profile: Profile | None = None,
    deliver: bool = True,
    now: datetime | None = None,
) -> DigestReport:
    """Build (or rebuild in place) the digest of the current period, then hand it to delivery.

    Writes one ``job_runs`` row (job='digest', source_id NULL) whose counts include ``digest_id``.
    * ``LLM_PROVIDER=none`` is fine: the digest is built and its health says the LLM is disabled.
    * ``config_error`` while building the client fails the run (``failed`` / ``config_error``).
    * any other exception is recorded as ``failed`` (error = the exception type name) and re-raised.
    """
    from sounding.digest import build_digest

    started = datetime.now(UTC)

    def record(status: str, counts: dict[str, Any], error: str | None) -> None:
        with session_factory() as session:
            session.add(
                JobRun(job="digest", started_at=started, finished_at=datetime.now(UTC), status=status,
                       counts=counts or None, error=error)
            )
            session.commit()

    profile = profile if profile is not None else load_profile(settings.profile_path)
    try:
        client = client if client is not None else build_client(settings, session_factory)
    except LLMError as exc:
        code = "config_error" if exc.code == "config_error" else exc.code
        record("failed", {"reason": code}, code)
        return DigestReport("failed", code)
    try:
        result = build_digest(session_factory, settings, client=client, profile=profile, now=now)
    except Exception as exc:
        record("failed", {}, type(exc).__name__)
        raise
    delivery = deliver_digest(session_factory, settings, result.digest_id) if deliver else None
    counts: dict[str, Any] = {"digest_id": result.digest_id, "rebuilt": result.rebuilt, **result.counts}
    if delivery is not None:
        counts["delivery"] = delivery
    record("ok", counts, None)
    return DigestReport("ok", None, result.digest_id, result.rebuilt, result.counts, result.body, result.language, delivery)


# Advisory-lock keys, one per job (fixed so that every worker agrees).
LOCK_KEYS = {"fetch": 0x534F554E01, "retention": 0x534F554E02, "summarize": 0x534F554E03, "digest": 0x534F554E04}


@contextmanager
def advisory_lock(engine: Engine, job: str):
    """Hold ``pg_try_advisory_lock`` for ``job`` on a dedicated connection; yields whether it was taken."""
    key = LOCK_KEYS[job]
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        got = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar())
        try:
            yield got
        finally:
            if got:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})


def record_skipped(session_factory: SessionFactory, job: str, reason: str) -> None:
    now = datetime.now(UTC)
    with session_factory() as session:
        session.add(
            JobRun(job=job, started_at=now, finished_at=now, status="skipped", counts={"reason": reason}, error=reason)
        )
        session.commit()
