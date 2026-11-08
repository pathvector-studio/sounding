"""Jobs. ``run_fetch`` pulls every enabled source once and writes one ``job_runs`` row per source.

One source failing never stops the others. Error text stored in the database is a fixed token or
an exception type name, never server output (it could carry secrets or addresses).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from sounding.ancestors import complete_ancestors, merge_missing
from sounding.config import Settings
from sounding.ingest import ingest_items
from sounding.models import JobRun, Source
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
        if source.kind == "ietf_mail" and not settings.fetch_contact_is_set:
            return _finish(
                session, source, started=started, status="skipped", last_status="skipped",
                counts={"reason": "fetch_contact_not_set"}, error="fetch_contact_not_set",
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
) -> list[SourceOutcome]:
    """Sync the profile into ``sources``, then fetch each enabled source with a registered adapter."""
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
            outcomes.append(_fetch_one(session_factory, settings, registry[kind], source_id, now or datetime.now(UTC)))
    finally:
        if owned:
            for adapter in registry.values():
                close = getattr(adapter, "close", None)
                if callable(close):
                    close()
    return outcomes
