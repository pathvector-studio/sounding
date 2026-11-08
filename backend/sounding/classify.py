"""Auto-mail detection and procedural events (specs/phase1.md, "イベント").

Rules are deliberately minimal (subject prefixes and keywords), ported from the phase-0 spike.
"""

from __future__ import annotations

import email.message
import re
from dataclasses import dataclass

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from sounding.models import Event, Item

AUTO_SENDER_RE = re.compile(
    r"(noreply|no-reply|do-not-reply|internet-drafts@|datatracker|ietf-secretariat|"
    r"agenda@|minutes@|meetecho|notifications?@|autoreply)",
    re.IGNORECASE,
)
AUTO_SUBJECT_RE = re.compile(
    r"^\s*(\[[^\]]*\]\s*)*(i-d action|new version notification|"
    r"(automatic )?(out of office|autoreply|auto-reply)|"
    r"(draft )?(agenda|minutes)\b.*(posted|uploaded|available)|"
    r"(wg|document|protocol) action|last call:|undeliverable|delivery status)",
    re.IGNORECASE,
)

# Greedy on purpose: the match ends at the revision (-NN) because "." or a space ends it.
DRAFT_RE = re.compile(r"\bdraft-[a-z0-9]+(?:-[a-z0-9]+)*", re.IGNORECASE)
_BODY_HEAD_CHARS = 2000

_LEADING_NOISE_RE = re.compile(
    r"^\s*((\[[^\]]*\]|(re|fwd?|aw)\s*:)\s*)*", re.IGNORECASE
)

EVENT_KINDS = (
    "ietf.id_action",
    "ietf.wg_adoption_call",
    "ietf.last_call",
    "ietf.publication",
    "ietf.other_notice",
)


@dataclass(frozen=True)
class EventSpec:
    kind: str
    ref: str | None


def classify_auto(msg: email.message.EmailMessage) -> str | None:
    """Why a message looks machine-generated (``items.auto_reason``), or None for a person."""
    auto_sub = str(msg.get("Auto-Submitted", "")).strip().lower()
    if auto_sub and auto_sub != "no":
        return "auto_submitted_header"
    if str(msg.get("Precedence", "")).strip().lower() in ("bulk", "junk"):
        return "precedence_bulk"
    if msg.get("X-Auto-Response-Suppress"):
        return "auto_response_suppress"
    if AUTO_SENDER_RE.search(str(msg.get("From", ""))):
        return "bot_sender"
    if AUTO_SUBJECT_RE.search(str(msg.get("Subject", ""))):
        return "bot_subject"
    return None


def draft_ref(*texts: str | None) -> str | None:
    """First draft name in the given texts (lower case), else None."""
    for t in texts:
        if not t:
            continue
        m = DRAFT_RE.search(t)
        if m:
            return m.group(0).lower()
    return None


def _kind_for(subject: str) -> str:
    s = _LEADING_NOISE_RE.sub("", subject).lower()
    if s.startswith("i-d action"):
        return "ietf.id_action"
    if "call for adoption" in s or "adoption call" in s or "wg adoption" in s:
        return "ietf.wg_adoption_call"
    if (
        s.startswith("last call")
        or "wg last call" in s
        or "working group last call" in s
        or re.search(r"\bwglc\b", s)
        or "ietf last call" in s
    ):
        return "ietf.last_call"
    if re.match(r"rfc\s*\d+\s+on\b", s) or s.startswith(("protocol action", "document action")):
        return "ietf.publication"
    return "ietf.other_notice"


def event_for(item: Item, subject: str | None) -> EventSpec | None:
    """The event an auto-mail item stands for. Call it only for items with ``is_auto``.

    Unmatched subjects become ``ietf.other_notice``; None only when there is no subject at all.
    """
    subject = (subject or "").strip()
    if not subject:
        return None
    head = "\n".join((item.body_text or "")[:_BODY_HEAD_CHARS].splitlines()[:20])
    return EventSpec(kind=_kind_for(subject), ref=draft_ref(subject, head))


def record_event(session: Session, item: Item, spec: EventSpec) -> bool:
    """Insert the event for this item; a second call for the same (item, kind) does nothing.

    Returns True when a row was inserted.
    """
    session.flush()
    stmt = (
        insert(Event)
        .values(
            source_id=item.source_id,
            item_id=item.id,
            conversation_id=item.conversation_id,
            kind=spec.kind,
            ref=spec.ref,
            occurred_at=item.published_at,
        )
        .on_conflict_do_nothing(index_elements=["item_id", "kind"])
        .returning(Event.id)
    )
    return session.execute(stmt).scalar_one_or_none() is not None
