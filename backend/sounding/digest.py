"""Build one digest (spec phase 3, R5): the period, the selection and ``digests.body_json``.

Period (deterministic, so rebuilding it never makes a second digest):
* ``period_end`` = the latest fire time at or before ``now`` of ``profile.digest.schedule`` (a cron
  string read in the ``TZ`` setting);
* ``period_start`` = the ``period_end`` of the latest earlier digest of the language, else ``period_end - 24h``.
A digest for the same (period_end, language) is rebuilt in place: same id, selections and ``body_json``
recomputed, its ``deliveries`` rows untouched (so nothing is sent twice). A manual run before today's
scheduled time therefore rebuilds the previous digest.

``body_json`` holds summaries and links only, never message or article bodies. Shape (all keys always present)::

    {"period": {"start", "end", "tz"}, "language", "generated_at", "quiet": bool,
     "health": [{"kind", "detail"}],
     "conversations": [{"id", "list", "subject", "state": {"version", "status_line", "changed_since_last"} | null,
                        "new_messages", "participants": [name], "score", "web_url", "latest_url"}],
     "events": [{"kind", "label", "ref", "title", "occurred_at", "url", "list"}],
     "items": [{"id", "title", "url", "feed", "summary" | null}],
     "counts": {...}}
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from sounding.classify import EVENT_KINDS
from sounding.config import Settings
from sounding.llm import LLMClient
from sounding.models import (
    Conversation, ConversationState, Digest, Event, Item, ItemSummary, JobRun, Selection, Source,
)
from sounding.profile import Profile
from sounding.select import select_for_digest
from sounding.text import mask_addresses

log = logging.getLogger(__name__)

DEFAULT_PERIOD = timedelta(hours=24)
FIRE_LOOKBACK = timedelta(days=8)
MAX_EVENTS = 50
MAX_PARTICIPANTS = 8

EVENT_LABELS = {
    "en": {
        "ietf.id_action": "New draft or revision", "ietf.wg_adoption_call": "WG adoption call",
        "ietf.last_call": "Last Call", "ietf.publication": "Published / approved", "ietf.other_notice": "Other notice",
    },
    "ja": {
        "ietf.id_action": "I-D の公開・更新", "ietf.wg_adoption_call": "WG 採択の呼びかけ",
        "ietf.last_call": "Last Call", "ietf.publication": "公開・承認", "ietf.other_notice": "その他のお知らせ",
    },
}
assert set(EVENT_LABELS["en"]) == set(EVENT_KINDS)

SessionFactory = Callable[[], Session]


@dataclass
class DigestResult:
    digest_id: int
    rebuilt: bool
    period_start: datetime
    period_end: datetime
    language: str
    counts: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)


def _lang(language: str) -> str:
    return language.split("-")[0].lower() if language.split("-")[0].lower() in EVENT_LABELS else "en"


# --- period -------------------------------------------------------------------------------------

def latest_fire_time(schedule: str, tz: str, now: datetime) -> datetime:
    """The latest fire time <= ``now`` of the cron ``schedule`` in ``tz`` (aware, in ``tz``).

    Steps the trigger forward from 8 days before ``now``. Raises ValueError for a bad cron string or
    when it never fires in that window."""
    zone = ZoneInfo(tz)
    trigger = CronTrigger.from_crontab(schedule, timezone=zone)
    now = now.astimezone(zone)
    prev: datetime | None = None
    cur = trigger.get_next_fire_time(None, now - FIRE_LOOKBACK)
    while cur is not None and cur <= now:
        prev = cur
        cur = trigger.get_next_fire_time(cur, cur)
    if prev is None:
        raise ValueError("digest schedule did not fire in the last 8 days")
    return prev


def digest_period(session: Session, profile: Profile, settings: Settings, now: datetime) -> tuple[datetime, datetime, Digest | None]:
    """``(start, end, existing)``: ``existing`` is the digest already made for (end, language), if any."""
    language = profile.profile.language
    end = latest_fire_time(profile.profile.digest.schedule, settings.tz, now).astimezone(UTC)
    existing = session.scalar(select(Digest).where(Digest.period_end == end, Digest.language == language))
    if existing is not None:
        return existing.period_start, end, existing
    prior_end = session.scalar(
        select(func.max(Digest.period_end)).where(Digest.language == language, Digest.period_end < end)
    )
    return (prior_end or end - DEFAULT_PERIOD), end, None


# --- body ---------------------------------------------------------------------------------------

def _health(session: Session, client: LLMClient | None, start: datetime, end: datetime) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    failed: dict[tuple[str, str], int] = {}
    for run in session.scalars(
        select(JobRun).where(JobRun.status == "failed", JobRun.started_at >= start, JobRun.started_at < end).order_by(JobRun.id)
    ):
        key = (run.job, run.error or "failed")
        failed[key] = failed.get(key, 0) + 1
    for (job, error), n in failed.items():
        out.append({"kind": "job_failed", "detail": f"{job}: {error}" + (f" x{n}" if n > 1 else "")})
    skipped = 0
    for run in session.scalars(
        select(JobRun).where(JobRun.job == "summarize", JobRun.started_at >= start, JobRun.started_at < end)
    ):
        skipped += int((run.counts or {}).get("budget_skipped") or 0)
    if skipped:
        out.append({"kind": "budget_skipped", "detail": str(skipped)})
    if client is None:
        out.append({"kind": "llm_disabled", "detail": ""})
    return out


def _conversations(
    session: Session, settings: Settings, digest_id: int, ids: list[int], language: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    if not ids:
        return []
    convs = {c.id: (c, key) for c, key in session.execute(
        select(Conversation, Source.key).join(Source, Source.id == Conversation.source_id).where(Conversation.id.in_(ids))
    )}
    scores = {
        s.target_id: s.score for s in session.scalars(
            select(Selection).where(Selection.digest_id == digest_id, Selection.target_kind == "conversation", Selection.included.is_(True))
        )
    }
    humans: dict[int, list[Item]] = {}
    for it in session.scalars(
        select(Item).where(Item.conversation_id.in_(ids), Item.is_auto.is_(False)).order_by(Item.published_at, Item.id)
    ):
        humans.setdefault(it.conversation_id, []).append(it)  # type: ignore[arg-type]
    out = []
    for cid in ids:
        if cid not in convs:
            continue
        conv, list_key = convs[cid]
        items = humans.get(cid, [])
        fresh = [i for i in items if start <= i.published_at < end]
        names: list[str] = []
        for i in fresh:
            name = mask_addresses(i.author_name) if i.author_name else None
            if name and name not in names:
                names.append(name)
        state = session.scalar(
            select(ConversationState)
            .where(ConversationState.conversation_id == cid, ConversationState.language == language)
            .order_by(ConversationState.version.desc()).limit(1)
        )
        latest = next((i.url for i in reversed(items) if i.url), None)
        out.append({
            "id": cid, "list": list_key, "subject": mask_addresses(conv.subject),
            "state": None if state is None else {
                "version": state.version, "status_line": mask_addresses(state.state_json["status_line"]),
                "changed_since_last": mask_addresses(state.changed_since_last) if state.changed_since_last else None,
            },
            "new_messages": len(fresh), "participants": names[:MAX_PARTICIPANTS], "score": scores.get(cid, 0.0),
            "web_url": f"{settings.public_base_url}/conversations/{cid}", "latest_url": latest,
        })
    return out


def _events(session: Session, profile: Profile, language: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Events in the period whose list is a working group of the profile, or whose draft ref starts with a tracked draft."""
    wg = set(profile.ietf.working_groups)
    prefixes = tuple(p.strip().lower() for p in profile.ietf.drafts if p.strip())
    labels = EVENT_LABELS[_lang(language)]
    out = []
    rows = session.execute(
        select(Event, Item, Source.key)
        .join(Source, Source.id == Event.source_id)
        .outerjoin(Item, Item.id == Event.item_id)
        .where(Event.occurred_at >= start, Event.occurred_at < end)
        .order_by(Event.occurred_at, Event.id)
    ).all()
    for ev, item, list_key in rows:
        if item is None:
            continue
        tracked = list_key in wg or bool(prefixes and ev.ref and ev.ref.lower().startswith(prefixes))
        if not tracked:
            continue
        out.append({
            "kind": ev.kind, "label": labels.get(ev.kind, ev.kind), "ref": ev.ref, "title": mask_addresses(item.title),
            "occurred_at": ev.occurred_at.astimezone(UTC).isoformat(timespec="minutes"), "url": item.url, "list": list_key,
        })
    return out


def _items(session: Session, ids: list[int], language: str) -> list[dict[str, Any]]:
    if not ids:
        return []
    rows = {it.id: (it, key) for it, key in session.execute(
        select(Item, Source.key).join(Source, Source.id == Item.source_id).where(Item.id.in_(ids))
    )}
    summaries = {
        s.item_id: s.summary for s in session.scalars(
            select(ItemSummary).where(ItemSummary.item_id.in_(ids), ItemSummary.language == language)
        )
    }
    return [
        {
            "id": i, "title": mask_addresses(rows[i][0].title), "url": rows[i][0].url, "feed": rows[i][1],
            "summary": mask_addresses(summaries[i]) if i in summaries else None,
        }
        for i in ids if i in rows
    ]


def build_digest(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    client: LLMClient | None,
    profile: Profile,
    now: datetime | None = None,
) -> DigestResult:
    """Create (or rebuild in place) the digest for the current period and return its id and counts.

    ``client`` None means ``LLM_PROVIDER=none``: the digest is still built and its health says so.
    A brand-new row is removed again if the build fails, so no empty digest is left behind."""
    now = now or datetime.now(UTC)
    language = profile.profile.language
    zone = ZoneInfo(settings.tz)
    with session_factory() as s:
        start, end, existing = digest_period(s, profile, settings, now)
        if existing is None:
            row = Digest(period_start=start, period_end=end, language=language, body_json={})
            s.add(row)
            s.commit()
            digest_id = row.id
        else:
            digest_id = existing.id
    created = existing is None
    try:
        res = select_for_digest(session_factory, client, profile, digest_id=digest_id, start=start, end=end, now=now)
        with session_factory() as s:
            convs = _conversations(s, settings, digest_id, res.conversation_ids, language, start, end)
            events = _events(s, profile, language, start, end)
            items = _items(s, res.item_ids, language)
            health = _health(s, client, start, end)
            counts = {
                "conversations": len(convs), "items": len(items), "events": min(len(events), MAX_EVENTS),
                "events_total": len(events), "health": len(health),
                "conversation_candidates": res.counts.get("conversation_candidates", 0),
                "item_candidates": res.counts.get("item_candidates", 0),
                "items_with_summary": sum(1 for i in items if i["summary"]),
            }
            body = {
                "period": {"start": start.astimezone(zone).isoformat(timespec="minutes"),
                           "end": end.astimezone(zone).isoformat(timespec="minutes"), "tz": settings.tz},
                "language": language, "generated_at": now.astimezone(UTC).isoformat(timespec="seconds"),
                "quiet": not (convs or events or items), "health": health, "conversations": convs,
                "events": events[:MAX_EVENTS], "items": items, "counts": counts,
            }
            s.execute(Digest.__table__.update().where(Digest.id == digest_id).values(body_json=body))
            s.commit()
    except BaseException:
        if created:
            with session_factory() as s:
                s.execute(delete(Selection).where(Selection.digest_id == digest_id))
                s.execute(delete(Digest).where(Digest.id == digest_id))
                s.commit()
        raise
    log.info("digest %d %s: %s", digest_id, "created" if created else "rebuilt", counts)
    return DigestResult(digest_id, not created, start, end, language, counts, body)


# --- text ---------------------------------------------------------------------------------------

_TEXT = {
    "en": {
        "title": "Sounding digest", "health": "Health", "conversations": "Active conversations",
        "events": "Procedural updates", "items": "Related articles", "quiet": "No activity in this period (the digest ran normally).",
        "new": "{n} new messages", "changed": "Changed since last", "no_state": "(no summary yet)", "latest": "Latest message",
        "web": "Details", "participants": "Participants",
        "job_failed": "Job failed: {d}", "budget_skipped": "{d} conversation(s) not summarized: daily LLM budget exceeded",
        "llm_disabled": "LLM is disabled (LLM_PROVIDER=none): no summaries were made",
    },
    "ja": {
        "title": "Sounding ダイジェスト", "health": "健康状態", "conversations": "動きがあった会話",
        "events": "手続きの更新", "items": "関連記事", "quiet": "この期間は動きがありませんでした（ダイジェストは正常に動いています）。",
        "new": "新着 {n} 件", "changed": "前回から", "no_state": "（まだ要約なし）", "latest": "最新のメッセージ",
        "web": "詳細", "participants": "参加者",
        "job_failed": "ジョブが失敗: {d}", "budget_skipped": "{d} 件の会話を要約していません（LLM の日次予算超過）",
        "llm_disabled": "LLM が無効です（LLM_PROVIDER=none）。要約は作られていません",
    },
}


def _when(iso: str) -> str:
    return iso.replace("T", " ")[:16]


def render_digest_text(body: dict[str, Any], language: str) -> str:
    """Plain text of a digest body in ``ja`` or ``en`` (anything else renders as ``en``). Self-contained."""
    t = _TEXT[_lang(language)]
    labels = EVENT_LABELS[_lang(language)]
    p = body["period"]
    lines = [f"{t['title']}  {_when(p['start'])} - {_when(p['end'])} ({p['tz']})"]
    if body.get("health"):
        lines += ["", f"[{t['health']}]"]
        for h in body["health"]:
            lines.append("- " + t.get(h["kind"], h["kind"]).format(d=h.get("detail", "")))
    if body.get("quiet"):
        lines += ["", t["quiet"]]
    if body.get("conversations"):
        lines += ["", f"[{t['conversations']}]"]
        for c in body["conversations"]:
            lines += ["", f"* {c['subject']}  ({c['list']}, {t['new'].format(n=c['new_messages'])})"]
            st = c.get("state")
            if st:
                lines.append(f"  {st['status_line']}")
                if st.get("changed_since_last"):
                    lines.append(f"  {t['changed']}: {st['changed_since_last']}")
            else:
                lines.append(f"  {t['no_state']}")
            if c.get("participants"):
                lines.append(f"  {t['participants']}: {', '.join(c['participants'])}")
            lines.append(f"  {t['web']}: {c['web_url']}")
            if c.get("latest_url"):
                lines.append(f"  {t['latest']}: {c['latest_url']}")
    if body.get("events"):
        lines += ["", f"[{t['events']}]"]
        for e in body["events"]:
            ref = f" {e['ref']}" if e.get("ref") else ""
            lines.append(f"- {labels.get(e['kind'], e['kind'])}{ref}: {e['title']} ({_when(e['occurred_at'])})")
            if e.get("url"):
                lines.append(f"  {e['url']}")
    if body.get("items"):
        lines += ["", f"[{t['items']}]"]
        for i in body["items"]:
            lines.append(f"- {i['title']}")
            if i.get("summary"):
                lines.append(f"  {i['summary']}")
            if i.get("url"):
                lines.append(f"  {i['url']}")
    return "\n".join(lines)
