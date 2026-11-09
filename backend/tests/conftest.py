import os

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")

# Must be set before `sounding` modules are imported (settings are cached).
os.environ["DATABASE_URL"] = TEST_DB or "postgresql+psycopg://u:p@localhost:1/none"
os.environ["IDENTITY_SECRET"] = "test-identity-secret-" + "x" * 32
os.environ["FETCH_CONTACT"] = "tester@example.com"


def _reset_schema_and_migrate() -> None:
    from sqlalchemy import text

    from sounding.cli import run_migrations
    from sounding.db import get_engine

    with get_engine().begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    run_migrations()


@pytest.fixture(scope="session")
def infra():
    if not TEST_DB:
        pytest.skip("TEST_DATABASE_URL not set")
    from sqlalchemy import text

    from sounding.db import dispose_engine, get_engine

    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"test database unreachable: {type(exc).__name__}")
    _reset_schema_and_migrate()
    yield
    dispose_engine()


@pytest.fixture
def session(infra):
    from sqlalchemy import text

    from sounding.db import get_engine, get_session_factory

    with get_engine().begin() as conn:
        conn.execute(
            text("TRUNCATE selections, deliveries, digests, item_summaries, conversation_states, llm_calls, job_runs, events, items, conversations, sources RESTART IDENTITY CASCADE")
        )
    with get_session_factory()() as s:
        yield s
        s.rollback()
