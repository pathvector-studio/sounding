"""Assign items to conversations incrementally (specs/phase1.md, "会話への割り当て").

Ported from the phase-0 spike (JWZ-style linking by Message-ID, plus a base-subject fallback),
rewritten so one new item at a time is placed against what is already in the database.

Rules, in order, for a just-inserted IMAP-kind item:
1. Message-IDs in In-Reply-To / References that match an existing item of the same source, or
   existing items that reference this item's Message-ID (children that arrived first), decide the
   conversation. Several distinct conversations are merged into the oldest one.
2. Otherwise, a conversation of the same source with the same base subject whose last message
   is within ``subject_window_days`` is joined.
3. Otherwise a new conversation is created.

RSS items never get a conversation. Nothing here reads or writes addresses.
"""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import any_, func, literal, or_, select, update
from sqlalchemy.orm import Session

from sounding.models import Conversation, Item, Source

# IETF / mailing-list specific knowledge lives here (and is passed in as parameters).
# Any leading [blob] is stripped anyway (list tags, "[Internet]" added by gateways); the tag list
# is kept for callers that want to be explicit.
DEFAULT_LIST_TAGS: tuple[str, ...] = ()
# Re/Fwd and localized MUA prefixes (de, sv, fr, pt, zh).
DEFAULT_REPLY_PREFIXES: tuple[str, ...] = (
    "re", "fwd", "fw", "aw", "sv", "vs", "tr", "ref", "antw", "答复", "回复", "回覆", "转发", "轉寄",
)  # fmt: skip

SUBJECT_WINDOW_DAYS = 30
_MAX_BASE_SUBJECT = 500
_MAX_REFS = 200


def normalize_msgid(value: str | None) -> str | None:
    """``<Foo@Example.com>`` -> ``foo@example.com`` (lower case, no angle brackets)."""
    if not value:
        return None
    m = re.search(r"<([^>]+)>", value)
    out = (m.group(1) if m else value).strip().lower()
    return out or None


def base_subject(
    subject: str | None,
    list_tags: tuple[str, ...] | list[str] = DEFAULT_LIST_TAGS,
    reply_prefixes: tuple[str, ...] | list[str] = DEFAULT_REPLY_PREFIXES,
) -> str:
    """RFC 5256-style base subject, lower-cased for comparison.

    Strips reply/forward prefixes (``Re:``, ``答复:``, ``AW[2]:`` ...), trailing ``(fwd)`` and any
    leading ``[...]`` blob. ``list_tags`` are removed as well, whether or not bracketed.
    """
    s = re.sub(r"\s+", " ", str(subject or "")).strip()
    prefix_re = re.compile(
        r"^\s*(" + "|".join(re.escape(p) for p in reply_prefixes) + r")(\[\d+\])?\s*[:：]\s*",
        re.IGNORECASE,
    )
    fwd_re = re.compile(r"\s*\(fwd\)\s*$", re.IGNORECASE)
    blob_re = re.compile(r"^\s*\[[^\]]*\]\s*(?=\S)")
    tags = [t.lower() for t in list_tags if t]
    while True:
        prev = s
        s = fwd_re.sub("", s)
        m = prefix_re.match(s)
        if m:
            s = s[m.end() :]
        m = blob_re.match(s)
        if m:
            s = s[m.end() :]
        for t in tags:
            if s.lower().startswith(t + ":"):
                s = s[len(t) + 1 :].lstrip()
        if s == prev:
            break
    return s.strip().lower()[:_MAX_BASE_SUBJECT]


def resolve(session: Session, conversation_id: int) -> int:
    """Follow ``merged_into`` to the final conversation (loop-safe)."""
    seen: set[int] = set()
    cur = conversation_id
    while cur not in seen:
        seen.add(cur)
        nxt = session.execute(
            select(Conversation.merged_into).where(Conversation.id == cur)
        ).scalar_one_or_none()
        if nxt is None:
            return cur
        cur = nxt
    return cur  # a merged_into cycle should never exist; stop rather than spin


def _referenced_ids(item: Item) -> list[str]:
    ids: list[str] = []
    for raw in [*(item.references or [])[:_MAX_REFS], item.in_reply_to]:
        n = normalize_msgid(raw)
        if n and n != item.message_id and n not in ids:
            ids.append(n)
    return ids


def _candidate_conversations(session: Session, item: Item) -> list[int]:
    """Final conversation ids reachable through parents and already-stored children."""
    found: set[int] = set()
    refs = _referenced_ids(item)
    if refs:
        rows = session.execute(
            select(Item.id, Item.message_id, Item.conversation_id)
            .where(
                Item.source_id == item.source_id,
                Item.message_id.in_(refs),
                Item.id != item.id,
                Item.conversation_id.is_not(None),
            )
            .order_by(Item.id)
        ).all()
        first_by_mid: dict[str, int] = {}  # duplicate Message-IDs: trust the earliest item only
        for _id, mid, conv in rows:
            first_by_mid.setdefault(mid, conv)
        found.update(first_by_mid.values())
    if item.message_id:
        mid = item.message_id
        children = session.execute(
            select(Item.conversation_id)
            .where(
                Item.source_id == item.source_id,
                Item.id != item.id,
                Item.conversation_id.is_not(None),
                or_(Item.in_reply_to == mid, literal(mid) == any_(Item.references)),
            )
            .distinct()
        ).scalars()
        found.update(children)
    return sorted({resolve(session, c) for c in found})


def _recompute(session: Session, conv_id: int) -> None:
    """Refresh count, first/last, root and subject of a conversation from its items."""
    count, first, last = session.execute(
        select(func.count(), func.min(Item.published_at), func.max(Item.published_at)).where(
            Item.conversation_id == conv_id
        )
    ).one()
    conv = session.get(Conversation, conv_id)
    assert conv is not None
    conv.message_count = count
    if count:
        conv.first_message_at = first
        conv.last_message_at = last
        root = session.execute(
            select(Item)
            .where(Item.conversation_id == conv_id)
            .order_by(
                # a message that replies to nothing is the natural root; else the earliest
                (Item.in_reply_to.is_not(None) | (func.coalesce(func.cardinality(Item.references), 0) > 0)),
                Item.published_at,
                Item.id,
            )
            .limit(1)
        ).scalar_one()
        conv.root_item_id = root.id
        conv.subject = root.title
        conv.base_subject = root.base_subject
    conv.updated_at = func.now()
    session.flush()


def _merge(session: Session, target_id: int, others: list[int]) -> None:
    """Merge ``others`` into ``target_id`` (the oldest)."""
    if not others:
        return
    session.execute(update(Item).where(Item.conversation_id.in_(others)).values(conversation_id=target_id))
    # conversations that were already merged into one of ``others`` now point at the target too
    session.execute(
        update(Conversation).where(Conversation.merged_into.in_(others)).values(merged_into=target_id)
    )
    session.execute(
        update(Conversation)
        .where(Conversation.id.in_(others))
        .values(merged_into=target_id, message_count=0, updated_at=func.now())
    )
    session.flush()


def assign(session: Session, item: Item, *, subject_window_days: int = SUBJECT_WINDOW_DAYS) -> int | None:
    """Place a just-inserted ``item`` in a conversation and return its (final) id.

    Returns None for items of a non-IMAP source (RSS items stay single). The item's
    ``message_id`` and ``base_subject`` are normalized here. The caller commits.
    """
    session.flush()  # the item needs an id; the caller may not have flushed
    source = session.get(Source, item.source_id)
    if source is None or source.kind != "ietf_mail":
        return None

    item.message_id = normalize_msgid(item.message_id)
    item.base_subject = base_subject(item.title)

    candidates = _candidate_conversations(session, item)
    if candidates:
        # oldest first; ties by id
        rows = session.execute(
            select(Conversation.id).where(Conversation.id.in_(candidates)).order_by(
                Conversation.first_message_at, Conversation.id
            )
        ).scalars().all()
        target = rows[0]
        _merge(session, target, [c for c in rows[1:]])
    else:
        target = None
        if item.base_subject:
            target = session.execute(
                select(Conversation.id)
                .where(
                    Conversation.source_id == item.source_id,
                    Conversation.base_subject == item.base_subject,
                    Conversation.merged_into.is_(None),
                    Conversation.last_message_at >= item.published_at - timedelta(days=subject_window_days),
                )
                .order_by(Conversation.last_message_at.desc(), Conversation.id)
                .limit(1)
            ).scalar_one_or_none()
        if target is None:
            conv = Conversation(
                source_id=item.source_id,
                subject=item.title,
                base_subject=item.base_subject,
                first_message_at=item.published_at,
                last_message_at=item.published_at,
                message_count=0,
            )
            session.add(conv)
            session.flush()
            target = conv.id

    item.conversation_id = target
    session.flush()
    _recompute(session, target)
    session.refresh(item)
    return target
