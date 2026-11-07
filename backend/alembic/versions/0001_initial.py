"""initial: sources, items, conversations, events, job_runs (the shared ingestion layer)

Revision ID: 0001
Revises:
Create Date: 2026-11-08
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("cursor", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        _ts("last_fetched_at", nullable=True),
        sa.Column("last_status", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _ts("created_at", server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('ietf_mail', 'rss')", name=op.f("ck_sources_kind")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
        sa.UniqueConstraint("kind", "key", name=op.f("uq_sources_kind_key")),
    )

    op.create_table(
        "conversations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("root_item_id", sa.BigInteger(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("base_subject", sa.Text(), nullable=True),
        _ts("first_message_at", nullable=False),
        _ts("last_message_at", nullable=False),
        sa.Column("message_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("merged_into", sa.BigInteger(), nullable=True),
        _ts("updated_at", server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], name=op.f("fk_conversations_source_id_sources")),
        sa.ForeignKeyConstraint(["merged_into"], ["conversations.id"], name=op.f("fk_conversations_merged_into_conversations")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(
        "ix_conversations_source_id_base_subject_last_message_at",
        "conversations",
        ["source_id", "base_subject", "last_message_at"],
    )

    op.create_table(
        "items",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("author_name", sa.Text(), nullable=True),
        sa.Column("author_key", sa.Text(), nullable=True),
        _ts("published_at", nullable=False),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("body_hash", sa.Text(), nullable=True),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("in_reply_to", sa.Text(), nullable=True),
        sa.Column("references", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("base_subject", sa.Text(), nullable=True),
        sa.Column("is_auto", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("auto_reason", sa.Text(), nullable=True),
        sa.Column("conversation_id", sa.BigInteger(), nullable=True),
        sa.Column("fetched_via", sa.Text(), server_default=sa.text("'poll'"), nullable=False),
        _ts("fetched_at", server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("fetched_via IN ('poll', 'ancestor')", name=op.f("ck_items_fetched_via")),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], name=op.f("fk_items_source_id_sources")),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], name=op.f("fk_items_conversation_id_conversations")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_items")),
        sa.UniqueConstraint("source_id", "external_id", name=op.f("uq_items_source_id_external_id")),
    )
    op.create_index("ix_items_message_id", "items", ["message_id"])
    op.create_index("ix_items_conversation_id_published_at", "items", ["conversation_id", "published_at"])
    op.create_index("ix_items_author_key", "items", ["author_key"])

    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=True),
        sa.Column("conversation_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=True),
        _ts("occurred_at", nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        _ts("created_at", server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], name=op.f("fk_events_source_id_sources")),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], name=op.f("fk_events_item_id_items")),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], name=op.f("fk_events_conversation_id_conversations")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.UniqueConstraint("item_id", "kind", name=op.f("uq_events_item_id_kind")),
    )
    op.create_index("ix_events_ref_occurred_at", "events", ["ref", "occurred_at"])
    op.create_index("ix_events_kind_occurred_at", "events", ["kind", "occurred_at"])

    op.create_table(
        "job_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("job", sa.Text(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=True),
        _ts("started_at", nullable=False),
        _ts("finished_at", nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("counts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('ok', 'failed', 'skipped')", name=op.f("ck_job_runs_status")),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], name=op.f("fk_job_runs_source_id_sources")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_runs")),
    )


def downgrade() -> None:
    raise RuntimeError("migrations are forward-only")
