from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from sounding.db import get_engine
from sounding.models import Base

TABLES = {"sources", "items", "conversations", "events", "job_runs", "llm_calls", "conversation_states"}


def test_upgrade_on_empty_database(infra):
    from conftest import _reset_schema_and_migrate

    _reset_schema_and_migrate()  # drops the schema, then runs `alembic upgrade head`
    with get_engine().connect() as conn:
        assert TABLES <= set(inspect(conn).get_table_names())
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0002"


def test_migration_matches_models(infra):
    with get_engine().connect() as conn:
        mc = MigrationContext.configure(conn, opts={"compare_type": True, "compare_server_default": True})
        diff = compare_metadata(mc, Base.metadata)
    assert diff == [], diff


def test_no_user_columns():
    for table in Base.metadata.tables.values():
        assert not [c.name for c in table.columns if "user" in c.name], table.name
