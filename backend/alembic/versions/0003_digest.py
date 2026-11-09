"""item_summaries (shared) and digests, selections, deliveries (user layer, no user column yet)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-10
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    op.create_table(
        "item_summaries",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("llm_call_id", sa.BigInteger(), nullable=True),
        _ts("created_at", server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], name=op.f("fk_item_summaries_item_id_items")),
        sa.ForeignKeyConstraint(["llm_call_id"], ["llm_calls.id"], name=op.f("fk_item_summaries_llm_call_id_llm_calls")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_item_summaries")),
        sa.UniqueConstraint("item_id", "language", name=op.f("uq_item_summaries_item_id_language")),
    )

    op.create_table(
        "digests",
        sa.Column("id", sa.BigInteger(), nullable=False),
        _ts("period_start", nullable=False),
        _ts("period_end", nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("body_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        _ts("created_at", server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_digests")),
        sa.UniqueConstraint("period_start", "period_end", "language", name=op.f("uq_digests_period_start_period_end_language")),
    )

    op.create_table(
        "selections",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("digest_id", sa.BigInteger(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column("included", sa.Boolean(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("reasons_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=False),
        _ts("created_at", server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("target_kind IN ('conversation', 'item')", name=op.f("ck_selections_target_kind")),
        sa.CheckConstraint("decided_by IN ('rule', 'llm')", name=op.f("ck_selections_decided_by")),
        sa.ForeignKeyConstraint(["digest_id"], ["digests.id"], name=op.f("fk_selections_digest_id_digests")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_selections")),
    )
    op.create_index("ix_selections_digest_id", "selections", ["digest_id"])

    op.create_table(
        "deliveries",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("digest_id", sa.BigInteger(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _ts("last_attempt_at", nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["digest_id"], ["digests.id"], name=op.f("fk_deliveries_digest_id_digests")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deliveries")),
        sa.UniqueConstraint("digest_id", "channel", name=op.f("uq_deliveries_digest_id_channel")),
    )


def downgrade() -> None:
    raise RuntimeError("migrations are forward-only")
