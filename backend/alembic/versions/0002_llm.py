"""llm_calls (call log, reuse by hash, budget) and conversation_states (versions of "where it stands")

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-08
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.BigInteger(), nullable=False),
        _ts("created_at", server_default=sa.func.now(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), server_default=sa.text("0"), nullable=False),
        sa.Column("duration_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("response_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("conversation_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("tier IN ('cheap', 'main')", name=op.f("ck_llm_calls_tier")),
        sa.CheckConstraint("status IN ('ok', 'failed', 'reused')", name=op.f("ck_llm_calls_status")),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], name=op.f("fk_llm_calls_conversation_id_conversations"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
    )
    op.create_index("ix_llm_calls_request_hash", "llm_calls", ["request_hash"])
    op.create_index("ix_llm_calls_created_at", "llm_calls", ["created_at"])

    op.create_table(
        "conversation_states",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("changed_since_last", sa.Text(), nullable=True),
        sa.Column("covered_item_ids", postgresql.ARRAY(sa.BigInteger()), nullable=False),
        _ts("last_item_published_at", nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("llm_call_id", sa.BigInteger(), nullable=True),
        _ts("created_at", server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], name=op.f("fk_conversation_states_conversation_id_conversations")
        ),
        sa.ForeignKeyConstraint(["llm_call_id"], ["llm_calls.id"], name=op.f("fk_conversation_states_llm_call_id_llm_calls")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_states")),
        sa.UniqueConstraint(
            "conversation_id", "language", "version", name=op.f("uq_conversation_states_conversation_id_language_version")
        ),
    )
    op.create_index(
        "ix_conversation_states_conversation_id_language_version_desc",
        "conversation_states",
        ["conversation_id", "language", sa.text("version DESC")],
    )


def downgrade() -> None:
    raise RuntimeError("migrations are forward-only")
