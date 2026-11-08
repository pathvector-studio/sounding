"""Store adapter items idempotently, place them in conversations and record auto-mail events.

Source-independent: nothing here knows about IMAP. The unique key (source_id, external_id) makes
a repeated fetch a no-op.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from sounding import classify, conversations
from sounding.models import Item as ItemRow
from sounding.models import Source
from sounding.sources.base import Item


@dataclass
class IngestResult:
    new: list[ItemRow] = field(default_factory=list)
    duplicates: int = 0
    events: int = 0


def body_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def ingest_items(session: Session, source: Source, items: Sequence[Item], *, fetched_via: str = "poll") -> IngestResult:
    """Insert ``items`` (oldest first), assign conversations, and record events for auto mail."""
    result = IngestResult()
    for it in sorted(items, key=lambda i: (i.published_at, i.external_id)):
        hints = it.conversation_hints
        stmt = (
            insert(ItemRow)
            .values(
                source_id=source.id,
                external_id=it.external_id,
                url=it.url,
                title=it.title,
                author_name=it.author_name,
                author_key=it.author_key,
                published_at=it.published_at,
                body_text=it.body_text,
                body_hash=body_hash(it.body_text),
                message_id=hints.message_id if hints else None,
                in_reply_to=hints.in_reply_to if hints else None,
                references=list(hints.references) if hints and hints.references else None,
                is_auto=it.is_auto,
                auto_reason=it.auto_reason,
                fetched_via=fetched_via,
            )
            .on_conflict_do_nothing(index_elements=["source_id", "external_id"])
            .returning(ItemRow.id)
        )
        item_id = session.execute(stmt).scalar_one_or_none()
        if item_id is None:
            result.duplicates += 1
            continue
        row = session.get(ItemRow, item_id)
        assert row is not None
        conversations.assign(session, row)
        if row.is_auto:
            spec = classify.event_for(row, row.title)
            if spec is not None and classify.record_event(session, row, spec):
                result.events += 1
        result.new.append(row)
    return result
