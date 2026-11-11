"""The web's read-only database role (migration 0004), the worker's password setting and the LAN guard."""

from __future__ import annotations

import psycopg.errors
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError

from sounding.config import Settings
from sounding.db import get_engine
from sounding.web.auth import WebConfigError, validate_bind
from sounding.worker import set_web_role_password, web_role_ready

from conftest import TEST_DB

PW = "web-role-test-pw"


def role_engine(password: str = PW):
    url = make_url(TEST_DB).set(username="sounding_web", password=password)
    return create_engine(url)


def make_settings(**kw) -> Settings:
    return Settings(_env_file=None, database_url=TEST_DB, **kw)


@pytest.fixture
def web_engine(infra):
    assert set_web_role_password(get_engine(), make_settings(web_db_password=PW))
    engine = role_engine()
    yield engine
    engine.dispose()


def test_role_can_select_every_table(web_engine):
    with get_engine().connect() as conn:
        tables = inspect(conn).get_table_names()
    assert "sources" in tables and "alembic_version" in tables
    with web_engine.connect() as conn:
        for table in tables:
            conn.execute(text(f'SELECT * FROM "{table}" LIMIT 1'))


@pytest.mark.parametrize(
    "stmt",
    [
        "INSERT INTO sources (kind, key) VALUES ('rss', 'x')",
        "UPDATE sources SET key = 'x'",
        "DELETE FROM sources",
        "TRUNCATE sources",
        "DROP TABLE sources",
        "CREATE TABLE evil (id int)",
        "ALTER ROLE sounding_web SUPERUSER",
    ],
)
def test_role_cannot_write(web_engine, stmt):
    with web_engine.connect() as conn:
        with pytest.raises(ProgrammingError) as exc:
            conn.execute(text(stmt))
    assert isinstance(exc.value.orig, psycopg.errors.InsufficientPrivilege), exc.value


def test_role_is_not_privileged(infra):
    with get_engine().connect() as conn:
        row = conn.execute(text(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolcanlogin FROM pg_roles WHERE rolname='sounding_web'"
        )).one()
    assert tuple(row) == (False, False, False, False, True)


def test_later_tables_are_readable_by_default(web_engine):
    with get_engine().begin() as conn:
        conn.execute(text("CREATE TABLE later_migration_table (id int)"))
    try:
        with web_engine.connect() as conn:
            conn.execute(text("SELECT * FROM later_migration_table"))
            with pytest.raises(ProgrammingError):
                conn.execute(text("INSERT INTO later_migration_table VALUES (1)"))
    finally:
        with get_engine().begin() as conn:
            conn.execute(text("DROP TABLE later_migration_table"))


def test_migration_is_idempotent_when_the_role_exists(infra):
    from conftest import _reset_schema_and_migrate

    _reset_schema_and_migrate()  # role survives DROP SCHEMA (cluster-wide); 0004 must not fail
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM pg_roles WHERE rolname='sounding_web'")).scalar() == 1


@pytest.mark.parametrize("password", ["it's-a-test", "back\\slash", "100%-pct", 'dq"uote', "a b;c--d"])
def test_worker_sets_password_with_special_characters(infra, password):
    assert set_web_role_password(get_engine(), make_settings(web_db_password=password))
    engine = role_engine(password)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT current_user")).scalar() == "sounding_web"
        wrong = role_engine("wrong")
        with pytest.raises(Exception):  # noqa: B017 - OperationalError: password authentication failed
            wrong.connect()
        wrong.dispose()
    finally:
        engine.dispose()


@pytest.mark.parametrize("value", ["", "change-me-web-db-password"])
def test_worker_skips_placeholder_password(infra, value, caplog):
    set_web_role_password(get_engine(), make_settings(web_db_password=PW))
    with caplog.at_level("WARNING"):
        assert set_web_role_password(get_engine(), make_settings(web_db_password=value)) is False
    assert "WEB_DB_PASSWORD" in caplog.text
    assert PW not in caplog.text
    # the previous password is untouched
    with role_engine().connect() as conn:
        conn.execute(text("SELECT 1"))


def test_password_is_never_logged(infra, caplog):
    with caplog.at_level("DEBUG"):
        set_web_role_password(get_engine(), make_settings(web_db_password="s3cret-value-xyz"))
    assert "s3cret-value-xyz" not in caplog.text


def test_web_role_ready_healthcheck(infra):
    settings = make_settings(web_db_password=PW)
    set_web_role_password(get_engine(), settings)
    assert web_role_ready(settings) is True
    assert web_role_ready(make_settings(web_db_password="wrong-password")) is False
    assert web_role_ready(make_settings(web_db_password="")) is False


@pytest.mark.parametrize("addr", ["", "127.0.0.1", "::1", "[::1]", "localhost", " LOCALHOST "])
def test_loopback_needs_no_password(addr):
    validate_bind(addr, "")


@pytest.mark.parametrize("addr", ["0.0.0.0", "192.168.1.10", "::", "example.com"])
def test_non_loopback_without_password_is_refused(addr):
    with pytest.raises(WebConfigError, match="WEB_PASSWORD_HASH"):
        validate_bind(addr, "")


def test_non_loopback_with_password_is_allowed():
    validate_bind("0.0.0.0", "scrypt$2$8$1$c2FsdA$a2V5")


def test_create_app_refuses_lan_without_password(tmp_path):
    from sounding.web.app import create_app

    prof = tmp_path / "profile.yaml"
    prof.write_text("profile:\n  language: en\n")
    kw = dict(database_url="postgresql+psycopg://u:p@localhost:1/none", session_secret="s" * 40, profile_path=str(prof))
    with pytest.raises(WebConfigError):
        create_app(Settings(_env_file=None, bind_address="0.0.0.0", **kw))
    create_app(Settings(_env_file=None, bind_address="127.0.0.1", **kw))
