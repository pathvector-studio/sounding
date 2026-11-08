from datetime import UTC, datetime
from email.message import EmailMessage

from sqlalchemy import inspect, text

from sounding import conversations as conv
from sounding.classify import classify_auto, event_for, record_event
from sounding.db import get_engine
from sounding.identity import author_key_for_email, display_name_for, sender_identity
from sounding.models import Base, Item, Source

SECRET = "unit-test-secret"


def test_same_address_same_key():
    k = author_key_for_email("alice@example.com", SECRET)
    assert k.startswith("mail:") and len(k) == len("mail:") + 32
    assert author_key_for_email("  Alice@Example.COM ", SECRET) == k
    assert author_key_for_email("bob@example.com", SECRET) != k


def test_secret_changes_key_and_empty_secret_gives_none():
    assert author_key_for_email("a@example.com", "s1") != author_key_for_email("a@example.com", "s2")
    assert author_key_for_email("a@example.com", "") is None
    assert author_key_for_email("a@example.com", None) is None
    assert author_key_for_email("", SECRET) is None


def test_display_name():
    assert display_name_for("Alice Example <alice@example.com>") == "Alice Example"
    assert display_name_for('"Example, Alice" <alice@example.com>') == "Example, Alice"
    assert display_name_for("=?utf-8?q?Ren=C3=A9?= <rene@example.com>") == "René"
    assert display_name_for("alice@example.com") == "a…"
    assert display_name_for("<alice@example.com>") == "a…"
    assert display_name_for("alice@example.com <alice@example.com>") == "a…"
    assert display_name_for("") is None and display_name_for(None) is None


def test_sender_identity():
    name, key = sender_identity("Alice Example <alice@example.com>", SECRET)
    assert name == "Alice Example" and key == author_key_for_email("alice@example.com", SECRET)


def test_raw_address_is_in_no_column(session):
    addr = "alice.private@example.com"
    s = Source(kind="ietf_mail", key="sidrops")
    session.add(s)
    session.flush()
    for n, subject in enumerate(["Topic", "Re: Topic", "I-D Action: draft-example-foo-01.txt"]):
        m = EmailMessage()
        m["From"] = f"{addr} <{addr}>" if n == 0 else f"Alice Example <{addr.upper()}>"
        m["Subject"] = subject
        name, key = sender_identity(m["From"], SECRET)
        it = Item(
            source_id=s.id, external_id=str(n), title=subject, author_name=name, author_key=key,
            published_at=datetime(2026, 10, 1, n, tzinfo=UTC), message_id=f"m{n}@example.com",
        )
        session.add(it)
        session.flush()
        conv.assign(session, it)
        reason = classify_auto(m)
        if reason:
            it.is_auto, it.auto_reason = True, reason
            record_event(session, it, event_for(it, subject))
    session.flush()
    insp = inspect(get_engine())
    checked = 0
    for table in Base.metadata.sorted_tables:
        assert insp.has_table(table.name)
        for col in table.columns:
            hit = session.execute(
                text(f'SELECT count(*) FROM "{table.name}" WHERE lower("{col.name}"::text) LIKE :p'),
                {"p": f"%{addr}%"},
            ).scalar_one()
            assert hit == 0, f"address found in {table.name}.{col.name}"
            checked += 1
    assert checked > 30
    assert session.execute(text("SELECT count(*) FROM items WHERE author_key IS NOT NULL")).scalar_one() == 3
