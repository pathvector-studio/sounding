"""Read-only queries shared by the JSON API and the HTML pages (spec phase 3, R7).

Every function returns plain dicts. The API serializes them and the templates render the same dicts.
Only summaries, titles, names and links are returned: never ``items.body_text`` (spec section 7).
Every URL that comes from a feed or a mail is passed through ``clean_url`` so a ``javascript:`` link
never leaves this module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sounding.config import Settings
from sounding.models import Conversation, ConversationState, Delivery, Digest, Event, Item, JobRun, Selection, Source
from sounding.safe import clean_body, clean_url, mask_deep as _mask  # noqa: F401 - re-exported for the web package
from sounding.text import mask_addresses

PAGE_SIZE = 30
RECENT_SELECTION_DIGESTS = 10
KNOWN_JOBS = ("fetch", "retention", "summarize", "digest")


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(UTC).isoformat(timespec="seconds")


# --- digests ------------------------------------------------------------------------------------

def _built():
    """A digest row is created empty and filled when its build finishes; only finished ones are shown."""
    return Digest.body_json.has_key("period")


def _digest_row(d: Digest, *, with_body: bool) -> dict[str, Any]:
    body = d.body_json or {}
    row: dict[str, Any] = {
        "id": d.id, "period_start": _iso(d.period_start), "period_end": _iso(d.period_end), "language": d.language,
        "created_at": _iso(d.created_at), "quiet": bool(body.get("quiet")), "counts": body.get("counts", {}),
    }
    if with_body:
        row["body"] = clean_body(body)
    return row


def latest_digest(session: Session) -> dict[str, Any] | None:
    d = session.scalar(select(Digest).where(_built()).order_by(Digest.period_end.desc(), Digest.id.desc()).limit(1))
    return None if d is None else _digest_row(d, with_body=True)


def digest_by_id(session: Session, digest_id: int) -> dict[str, Any] | None:
    d = session.scalar(select(Digest).where(Digest.id == digest_id, _built()))
    return None if d is None else _digest_row(d, with_body=True)


def digest_list(session: Session, page: int = 1, size: int = PAGE_SIZE) -> dict[str, Any]:
    page = max(page, 1)
    rows = session.scalars(
        select(Digest).where(_built()).order_by(Digest.period_end.desc(), Digest.id.desc()).offset((page - 1) * size).limit(size + 1)
    ).all()
    return {"page": page, "size": size, "has_more": len(rows) > size, "digests": [_digest_row(d, with_body=False) for d in rows[:size]]}


def recent_digests(session: Session, n: int) -> list[Digest]:
    """The ORM rows for the Atom feed (the caller renders their bodies)."""
    return list(session.scalars(select(Digest).where(_built()).order_by(Digest.period_end.desc(), Digest.id.desc()).limit(n)))


# --- conversation -------------------------------------------------------------------------------

def _state_view(row: ConversationState) -> dict[str, Any]:
    st = _mask(row.state_json)
    sources = []
    for s in st.get("sources", []):
        sources.append({**s, "url": clean_url(s.get("url"))})
    return {
        "version": row.version, "language": row.language, "created_at": _iso(row.created_at), "model": row.model,
        "status_line": st["status_line"], "issues": st["issues"], "agreed": st["agreed"],
        "open_questions": st["open_questions"], "next": st["next"],
        "changed_since_last": mask_addresses(row.changed_since_last) if row.changed_since_last else None,
        "sources": sources,
    }


def conversation_detail(session: Session, conversation_id: int, language: str) -> dict[str, Any] | None:
    conv = session.get(Conversation, conversation_id)
    if conv is None:
        return None
    source = session.get(Source, conv.source_id)
    states = session.scalars(
        select(ConversationState)
        .where(ConversationState.conversation_id == conversation_id, ConversationState.language == language)
        .order_by(ConversationState.version.desc())
    ).all()
    digest_ids = [
        r for r in session.scalars(select(Digest.id).where(_built()).order_by(Digest.period_end.desc(), Digest.id.desc()).limit(RECENT_SELECTION_DIGESTS))
    ]
    selections = []
    if digest_ids:
        rows = session.execute(
            select(Selection, Digest.period_end)
            .join(Digest, Digest.id == Selection.digest_id)
            .where(Selection.digest_id.in_(digest_ids), Selection.target_kind == "conversation", Selection.target_id == conversation_id)
            .order_by(Digest.period_end.desc(), Selection.id)
        ).all()
        selections = [
            {"digest_id": sel.digest_id, "period_end": _iso(end), "included": sel.included, "score": sel.score,
             "decided_by": sel.decided_by, "reasons": _mask(sel.reasons_json)}
            for sel, end in rows
        ]
    return {
        "id": conv.id, "list": source.key if source else None, "subject": mask_addresses(conv.subject),
        "first_message_at": _iso(conv.first_message_at), "last_message_at": _iso(conv.last_message_at),
        "message_count": conv.message_count, "merged_into": conv.merged_into, "language": language,
        "state": _state_view(states[0]) if states else None,
        "versions": [
            {"version": s.version, "created_at": _iso(s.created_at), "model": s.model,
             "changed_since_last": mask_addresses(s.changed_since_last) if s.changed_since_last else None}
            for s in states
        ],
        "selections": selections,
    }


# --- status -------------------------------------------------------------------------------------

def _counts(session: Session, model: type, column: Any) -> dict[int, int]:
    return {k: n for k, n in session.execute(select(column, func.count()).select_from(model).group_by(column))}


def status_info(session: Session, settings: Settings, atom_url: str) -> dict[str, Any]:
    from sounding.llm import calls_today, spend_today

    jobs = []
    names = list(dict.fromkeys([*KNOWN_JOBS, *session.scalars(select(JobRun.job).distinct().order_by(JobRun.job))]))
    for name in names:
        last_ok = session.scalar(
            select(JobRun).where(JobRun.job == name, JobRun.status == "ok").order_by(JobRun.started_at.desc(), JobRun.id.desc()).limit(1)
        )
        last_failed = session.scalar(
            select(JobRun).where(JobRun.job == name, JobRun.status == "failed").order_by(JobRun.started_at.desc(), JobRun.id.desc()).limit(1)
        )
        jobs.append({
            "job": name,
            "last_ok_at": _iso(last_ok.finished_at or last_ok.started_at) if last_ok else None,
            "last_failed_at": _iso(last_failed.started_at) if last_failed else None,
            "last_error": mask_addresses(last_failed.error) if last_failed and last_failed.error else None,
        })
    items = _counts(session, Item, Item.source_id)
    convs = _counts(session, Conversation, Conversation.source_id)
    events = _counts(session, Event, Event.source_id)
    sources = [
        {"kind": s.kind, "key": s.key, "enabled": s.enabled, "last_fetched_at": _iso(s.last_fetched_at),
         "last_status": s.last_status, "items": items.get(s.id, 0), "conversations": convs.get(s.id, 0), "events": events.get(s.id, 0)}
        for s in session.scalars(select(Source).order_by(Source.kind, Source.key))
    ]
    budget = settings.llm_daily_budget_usd
    calls = calls_today(session, settings.tz)
    last = session.scalar(select(Digest).where(_built()).order_by(Digest.period_end.desc(), Digest.id.desc()).limit(1))
    deliveries = []
    for channel in session.scalars(select(Delivery.channel).distinct().order_by(Delivery.channel)):
        d = session.scalar(
            select(Delivery).where(Delivery.channel == channel)
            .order_by(Delivery.last_attempt_at.desc().nulls_last(), Delivery.id.desc()).limit(1)
        )
        deliveries.append({
            "channel": channel, "status": d.status, "digest_id": d.digest_id, "attempts": d.attempts,
            "last_attempt_at": _iso(d.last_attempt_at), "error": d.error,  # a fixed token; never the URL or an address
        })
    return {
        "jobs": jobs,
        "deliveries": deliveries,
        "sources": sources,
        "llm": {
            "provider": settings.llm_provider, "tz": settings.tz,
            "spend_today_usd": f"{spend_today(session, settings.tz):.4f}",
            "budget_usd": f"{budget:.2f}" if budget > 0 else None,  # None = unlimited
            "calls_today": {k: calls.get(k, 0) for k in ("ok", "failed", "reused")},
        },
        "last_digest": None if last is None else _digest_row(last, with_body=False),
        "atom_url": atom_url,
    }
