"""The shared (ingestion) layer. No table here has a user column (specs/phase1.md, principle 1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


_NOW = func.now()


def _ts(**kw: Any) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kw)


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("kind", "key"),
        CheckConstraint("kind IN ('ietf_mail', 'rss')", name="kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)  # ietf_mail | rss
    key: Mapped[str] = mapped_column(Text)  # list name | feed URL
    url: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=true())
    cursor: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_fetched_at: Mapped[datetime | None] = _ts()
    last_status: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = _ts(server_default=_NOW)


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id"),
        CheckConstraint("fetched_via IN ('poll', 'ancestor')", name="fetched_via"),
        Index("ix_items_message_id", "message_id"),
        Index("ix_items_conversation_id_published_at", "conversation_id", "published_at"),
        Index("ix_items_author_key", "author_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    external_id: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    author_name: Mapped[str | None] = mapped_column(Text)
    author_key: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime] = _ts()
    body_text: Mapped[str | None] = mapped_column(Text)  # NULL after CONTENT_RETENTION_DAYS
    body_hash: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(Text)
    in_reply_to: Mapped[str | None] = mapped_column(Text)
    references: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    base_subject: Mapped[str | None] = mapped_column(Text)
    is_auto: Mapped[bool] = mapped_column(Boolean, server_default=false())
    auto_reason: Mapped[str | None] = mapped_column(Text)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"))
    fetched_via: Mapped[str] = mapped_column(Text, server_default=text("'poll'"))  # poll | ancestor
    fetched_at: Mapped[datetime] = _ts(server_default=_NOW)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_source_id_base_subject_last_message_at", "source_id", "base_subject", "last_message_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    # Deliberately no FK: items.conversation_id already points the other way.
    root_item_id: Mapped[int | None] = mapped_column(BigInteger)
    subject: Mapped[str] = mapped_column(Text)
    base_subject: Mapped[str | None] = mapped_column(Text)
    first_message_at: Mapped[datetime] = _ts()
    last_message_at: Mapped[datetime] = _ts()
    message_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    merged_into: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"))
    updated_at: Mapped[datetime] = _ts(server_default=_NOW)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("item_id", "kind"),
        Index("ix_events_ref_occurred_at", "ref", "occurred_at"),
        Index("ix_events_kind_occurred_at", "kind", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"))
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"))
    kind: Mapped[str] = mapped_column(Text)
    ref: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = _ts()
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _ts(server_default=_NOW)


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        CheckConstraint("status IN ('ok', 'failed', 'skipped')", name="status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job: Mapped[str] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"))
    started_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = _ts()
    status: Mapped[str] = mapped_column(Text)  # ok | failed | skipped
    counts: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
