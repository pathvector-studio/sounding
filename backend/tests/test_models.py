from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from sounding.models import Conversation, Event, Item, Source

NOW = datetime(2026, 11, 8, tzinfo=UTC)


def _source(session, key="sidrops", kind="ietf_mail") -> Source:
    s = Source(kind=kind, key=key)
    session.add(s)
    session.flush()
    return s


def _item(session, source, external_id="<a@example.com>") -> Item:
    i = Item(source_id=source.id, external_id=external_id, title="t", published_at=NOW)
    session.add(i)
    session.flush()
    return i


def test_defaults(session):
    s = _source(session)
    session.refresh(s)
    assert s.enabled is True and s.consecutive_failures == 0 and s.created_at is not None
    i = _item(session, s)
    session.refresh(i)
    assert i.is_auto is False and i.fetched_via == "poll" and i.fetched_at is not None


def test_source_kind_key_unique(session):
    _source(session)
    session.add(Source(kind="ietf_mail", key="sidrops"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_same_key_different_kind_ok(session):
    _source(session)
    _source(session, kind="rss")


def test_source_kind_checked(session):
    session.add(Source(kind="nntp", key="x"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_item_source_external_id_unique(session):
    s = _source(session)
    _item(session, s)
    session.add(Item(source_id=s.id, external_id="<a@example.com>", title="dup", published_at=NOW))
    with pytest.raises(IntegrityError):
        session.flush()


def test_item_external_id_may_repeat_across_sources(session):
    _item(session, _source(session, "a"))
    _item(session, _source(session, "b"))


def test_event_item_kind_unique(session):
    s = _source(session)
    i = _item(session, s)
    session.add(Event(source_id=s.id, item_id=i.id, kind="ietf.last_call", occurred_at=NOW))
    session.flush()
    session.add(Event(source_id=s.id, item_id=i.id, kind="ietf.last_call", occurred_at=NOW))
    with pytest.raises(IntegrityError):
        session.flush()


def test_event_other_kind_same_item_ok(session):
    s = _source(session)
    i = _item(session, s)
    session.add_all(
        [
            Event(source_id=s.id, item_id=i.id, kind="ietf.last_call", occurred_at=NOW),
            Event(source_id=s.id, item_id=i.id, kind="ietf.id_action", occurred_at=NOW),
        ]
    )
    session.flush()


def test_conversation_merged_into_fk(session):
    s = _source(session)
    old = Conversation(source_id=s.id, subject="a", first_message_at=NOW, last_message_at=NOW)
    session.add(old)
    session.flush()
    new = Conversation(source_id=s.id, subject="a", first_message_at=NOW, last_message_at=NOW, merged_into=old.id)
    session.add(new)
    session.flush()
    assert new.merged_into == old.id
    session.add(Conversation(source_id=s.id, subject="b", first_message_at=NOW, last_message_at=NOW, merged_into=999999))
    with pytest.raises(IntegrityError):
        session.flush()


def test_item_conversation_and_references_array(session):
    s = _source(session)
    c = Conversation(source_id=s.id, subject="a", first_message_at=NOW, last_message_at=NOW)
    session.add(c)
    session.flush()
    i = Item(
        source_id=s.id, external_id="<x@example.com>", title="t", published_at=NOW,
        conversation_id=c.id, references=["<a@example.com>", "<b@example.com>"],
    )
    session.add(i)
    session.flush()
    session.expire(i)
    assert i.references == ["<a@example.com>", "<b@example.com>"]
