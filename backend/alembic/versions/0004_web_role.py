"""sounding_web: a read-only database role for the web container (phase 4, R4)

The role has no password here: the worker sets it from WEB_DB_PASSWORD at every start. Roles are
cluster-wide, so creating it is idempotent (the test database is migrated again after DROP SCHEMA).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-11
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sounding_web') THEN
                CREATE ROLE sounding_web LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
            END IF;
        EXCEPTION WHEN duplicate_object THEN
            NULL;  -- created concurrently by a second migrating process
        END
        $$
        """
    )
    op.execute("DO $$ BEGIN EXECUTE format('GRANT CONNECT ON DATABASE %I TO sounding_web', current_database()); END $$")
    op.execute("GRANT USAGE ON SCHEMA public TO sounding_web")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO sounding_web")
    # Tables that later migrations create (as the migrating owner) are readable too. Nothing else is ever granted.
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO sounding_web")


def downgrade() -> None:
    raise NotImplementedError("migrations are forward-only")
