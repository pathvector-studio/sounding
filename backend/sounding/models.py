"""The shared (ingestion) layer. No table here has a user column (specs/phase1.md, principle 1)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
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


class LlmCall(Base):
    """One row per LLM call attempt (and per reuse). No prompt text is stored, only its hash."""

    __tablename__ = "llm_calls"
    __table_args__ = (
        CheckConstraint("tier IN ('cheap', 'main')", name="tier"),
        CheckConstraint("status IN ('ok', 'failed', 'reused')", name="status"),
        Index("ix_llm_calls_request_hash", "request_hash"),
        Index("ix_llm_calls_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = _ts(server_default=_NOW)
    purpose: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    tier: Mapped[str] = mapped_column(Text)  # cheap | main
    request_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)  # ok | failed | reused
    error: Mapped[str | None] = mapped_column(Text)  # a fixed code (sounding.llm.base), never SDK text
    input_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cache_write_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cache_read_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=text("0"))
    duration_ms: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    response_json: Mapped[Any | None] = mapped_column(JSONB)  # only for ok
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"))


class ConversationState(Base):
    """A version of a conversation's "where it stands", per output language (used from phase 2 item 3)."""

    __tablename__ = "conversation_states"
    __table_args__ = (UniqueConstraint("conversation_id", "language", "version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    language: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    state_json: Mapped[Any] = mapped_column(JSONB)
    changed_since_last: Mapped[str | None] = mapped_column(Text)
    covered_item_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))
    last_item_published_at: Mapped[datetime] = _ts()
    prompt_version: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    llm_call_id: Mapped[int | None] = mapped_column(ForeignKey("llm_calls.id"))
    created_at: Mapped[datetime] = _ts(server_default=_NOW)


Index(
    "ix_conversation_states_conversation_id_language_version_desc",
    ConversationState.conversation_id,
    ConversationState.language,
    ConversationState.version.desc(),
)


class ItemSummary(Base):
    """A short summary of one (RSS) item per output language. Shared layer: no user column."""

    __tablename__ = "item_summaries"
    __table_args__ = (UniqueConstraint("item_id", "language"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    language: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    llm_call_id: Mapped[int | None] = mapped_column(ForeignKey("llm_calls.id"))
    created_at: Mapped[datetime] = _ts(server_default=_NOW)


# --- The user layer (phase 3). Single user for now: deliberately no user_id column; it is added
# --- together with multi-user support (specs/phase3.md, out of scope).


class Digest(Base):
    __tablename__ = "digests"
    __table_args__ = (UniqueConstraint("period_start", "period_end", "language"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    period_start: Mapped[datetime] = _ts()
    period_end: Mapped[datetime] = _ts()
    language: Mapped[str] = mapped_column(Text)
    body_json: Mapped[Any] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _ts(server_default=_NOW)


class Selection(Base):
    """Why a conversation or item was (not) put into a digest: score, reasons and who decided."""

    __tablename__ = "selections"
    __table_args__ = (
        CheckConstraint("target_kind IN ('conversation', 'item')", name="target_kind"),
        CheckConstraint("decided_by IN ('rule', 'llm')", name="decided_by"),
        Index("ix_selections_digest_id", "digest_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    digest_id: Mapped[int] = mapped_column(ForeignKey("digests.id"))
    target_kind: Mapped[str] = mapped_column(Text)  # conversation | item
    target_id: Mapped[int] = mapped_column(BigInteger)  # no FK: the kind says which table
    included: Mapped[bool] = mapped_column(Boolean)
    score: Mapped[float] = mapped_column(Float)
    reasons_json: Mapped[Any] = mapped_column(JSONB)
    decided_by: Mapped[str] = mapped_column(Text)  # rule | llm
    created_at: Mapped[datetime] = _ts(server_default=_NOW)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("digest_id", "channel"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    digest_id: Mapped[int] = mapped_column(ForeignKey("digests.id"))
    channel: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_attempt_at: Mapped[datetime | None] = _ts()
    error: Mapped[str | None] = mapped_column(Text)
