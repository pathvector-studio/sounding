import pytest

from sounding.config import Settings


@pytest.fixture
def env(monkeypatch):
    for key in ("DATABASE_URL", "FETCH_CONTACT", "IDENTITY_SECRET", "IETF_BACKFILL_DAYS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:dbpass-dummy@db:5432/x")
    return monkeypatch


def test_defaults_and_new_keys(env):
    s = Settings()
    assert s.fetch_interval_minutes == 60
    assert s.content_retention_days == 90
    assert s.ietf_backfill_days == 30
    assert s.ietf_max_messages_per_run == 500
    assert s.ietf_ancestor_max_depth == 50
    assert s.ietf_ancestor_max_age_days == 365
    assert not s.fetch_contact_is_set and not s.identity_secret_is_set


def test_reads_environment_and_ignores_unrelated_keys(env):
    env.setenv("IETF_BACKFILL_DAYS", "7")
    env.setenv("POSTGRES_PASSWORD", "unrelated")
    env.setenv("FETCH_CONTACT", "you@example.com")
    s = Settings()
    assert s.ietf_backfill_days == 7
    assert s.fetch_contact_is_set


def test_placeholders_count_as_not_set(env):
    env.setenv("FETCH_CONTACT", "change-me@example.com")
    env.setenv("IDENTITY_SECRET", "change-me-identity-secret")
    s = Settings()
    assert not s.fetch_contact_is_set and not s.identity_secret_is_set


def test_secrets_are_hidden_in_repr(env):
    env.setenv("IDENTITY_SECRET", "s3cret-identity-value")
    env.setenv("FETCH_CONTACT", "private@example.com")
    s = Settings()
    shown = repr(s) + str(s) + str(s.model_dump())
    for value in ("s3cret-identity-value", "private@example.com", "dbpass-dummy"):
        assert value not in shown
    assert s.identity_secret.get_secret_value() == "s3cret-identity-value"


def test_identity_secret_is_set_property(env):
    for value, expected in (("", False), ("   ", False), ("change-me-x", False), ("0123abcd", True)):
        env.setenv("IDENTITY_SECRET", value)
        assert Settings().identity_secret_is_set is expected
