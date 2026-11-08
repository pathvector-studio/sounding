from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from sounding import conversations as conv
from sounding.models import Conversation, Item, Source

T0 = datetime(2026, 10, 1, 12, tzinfo=UTC)


@pytest.fixture
def src(session):
    s = Source(kind="ietf_mail", key="sidrops")
    session.add(s)
    session.flush()
    return s


_n = 0


def add(session, src, mid, subject="Topic", *, refs=None, irt=None, days=0.0, hours=0.0, assign=True):
    global _n
    _n += 1
    it = Item(
        source_id=src.id,
        external_id=f"ext-{_n}",
        title=subject,
        published_at=T0 + timedelta(days=days, hours=hours),
        message_id=mid,
        in_reply_to=irt,
        references=refs,
    )
    session.add(it)
    session.flush()
    if assign:
        conv.assign(session, it)
    return it


def final(session, it):
    session.refresh(it)
    return conv.resolve(session, it.conversation_id)


def test_base_subject():
    f = conv.base_subject
    assert f("Re: [sidrops] Hello  World") == "hello world"
    assert f("[Internet] Re: [sidrops] Hello World") == "hello world"
    assert f("答复: [sidrops] Hello World") == "hello world"
    assert f("回复: Re: Hello World") == "hello world"
    assert f("AW[2]: SV: Hello World (fwd)") == "hello world"
    assert f("Fwd: Hello World") == "hello world"
    assert f("") == "" and f(None) == ""
    assert f("Re: sidrops: topic", list_tags=["sidrops"]) == "topic"


def test_headers_threading(session, src):
    a = add(session, src, "<A@example.com>", "Topic")
    b = add(session, src, "b@example.com", "Different title", irt="<a@example.com>", refs=["a@example.com"], hours=1)
    c = add(session, src, "c@example.com", "Re: Topic", irt="b@example.com", refs=["a@example.com", "b@example.com"], hours=2)
    assert a.message_id == "a@example.com"
    assert a.conversation_id == b.conversation_id == c.conversation_id
    cv = session.get(Conversation, a.conversation_id)
    assert cv.message_count == 3
    assert cv.root_item_id == a.id
    assert cv.first_message_at == a.published_at and cv.last_message_at == c.published_at
    assert cv.subject == "Topic" and cv.base_subject == "topic"


def test_in_reply_to_only(session, src):
    a = add(session, src, "a@example.com", "Hello")
    b = add(session, src, "b@example.com", "Totally other", irt="<a@example.com>", hours=1)
    assert a.conversation_id == b.conversation_id


def test_subject_fallback_without_headers(session, src):
    a = add(session, src, "a@example.com", "Question about ASPA")
    b = add(session, src, "b@example.com", "Re: Question about ASPA", days=1)
    c = add(session, src, "c@example.com", "Another thing", days=1)
    assert a.conversation_id == b.conversation_id
    assert c.conversation_id != a.conversation_id


def test_gateway_tag_and_localized_prefix(session, src):
    a = add(session, src, "a@example.com", "[sidrops] Draft review")
    b = add(session, src, "b@example.com", "[Internet] 答复: [sidrops] Draft review", hours=1)
    assert a.conversation_id == b.conversation_id


def test_subject_window_expiry(session, src):
    a = add(session, src, "a@example.com", "Old topic")
    b = add(session, src, "b@example.com", "Re: Old topic", days=31)
    assert a.conversation_id != b.conversation_id
    c = add(session, src, "c@example.com", "Re: Old topic", days=31 + 29)
    assert c.conversation_id == b.conversation_id  # window counts from the last message


def test_empty_subject_does_not_merge(session, src):
    a = add(session, src, "a@example.com", "")
    b = add(session, src, "b@example.com", "Re:", hours=1)
    assert a.conversation_id != b.conversation_id


def test_parent_arrives_later(session, src):
    child = add(session, src, "b@example.com", "Re: Topic", irt="a@example.com", refs=["a@example.com"], hours=1)
    other = add(session, src, "x@example.com", "Unrelated", hours=2)
    parent = add(session, src, "a@example.com", "Entirely new subject", hours=0.5)
    session.refresh(child)
    assert parent.conversation_id == child.conversation_id
    assert parent.conversation_id != other.conversation_id
    cv = session.get(Conversation, parent.conversation_id)
    assert cv.message_count == 2 and cv.root_item_id == parent.id and cv.subject == "Entirely new subject"
    assert cv.first_message_at == parent.published_at


def test_parent_arrives_later_merges_two_conversations(session, src):
    c1 = add(session, src, "b1@example.com", "One", irt="a@example.com", days=1)
    c2 = add(session, src, "b2@example.com", "Two", irt="a@example.com", days=2)
    assert c1.conversation_id != c2.conversation_id
    p = add(session, src, "a@example.com", "Root", days=0)
    assert len({final(session, i) for i in (c1, c2, p)}) == 1
    assert session.get(Conversation, final(session, p)).message_count == 3


def test_merge_by_message_referencing_both(session, src):
    a = add(session, src, "a@example.com", "Alpha", days=0)
    b = add(session, src, "b@example.com", "Beta", days=1)
    ca, cb = a.conversation_id, b.conversation_id
    assert ca != cb
    m = add(session, src, "m@example.com", "Gamma", refs=["b@example.com", "a@example.com"], days=2)
    for i in (a, b, m):
        session.refresh(i)
    assert a.conversation_id == b.conversation_id == m.conversation_id == ca  # oldest wins
    old, new = session.get(Conversation, ca), session.get(Conversation, cb)
    assert new.merged_into == ca and new.message_count == 0 and old.merged_into is None
    assert old.message_count == 3 and old.root_item_id == a.id
    assert old.first_message_at == a.published_at and old.last_message_at == m.published_at
    # merged_into is followed
    assert conv.resolve(session, cb) == ca


def test_merge_chain_follows_to_final(session, src):
    a = add(session, src, "a@example.com", "Alpha", days=0)
    b = add(session, src, "b@example.com", "Beta", days=1)
    c = add(session, src, "c@example.com", "Gamma", days=2)
    add(session, src, "m1@example.com", "x", refs=["c@example.com", "b@example.com"], days=3)
    add(session, src, "m2@example.com", "y", refs=["b@example.com", "a@example.com"], days=4)
    ids = {final(session, i) for i in (a, b, c)}
    assert ids == {a.conversation_id}
    live = session.scalars(select(Conversation).where(Conversation.merged_into.is_(None))).all()
    assert len(live) == 1 and live[0].message_count == 5
    for cv in session.scalars(select(Conversation).where(Conversation.merged_into.is_not(None))):
        assert cv.merged_into == live[0].id


def test_references_loop(session, src):
    a = add(session, src, "a@example.com", "Loop", refs=["b@example.com"], days=0)
    b = add(session, src, "b@example.com", "Other name", refs=["a@example.com"], days=1)
    c = add(session, src, "c@example.com", "Self", refs=["c@example.com"], days=2)
    assert a.conversation_id == b.conversation_id
    assert c.conversation_id != a.conversation_id
    assert session.get(Conversation, a.conversation_id).message_count == 2


def test_duplicate_message_id(session, src):
    a = add(session, src, "a@example.com", "First", days=0)
    d = add(session, src, "a@example.com", "Different subject dup", days=1)
    reply = add(session, src, "r@example.com", "Re: First", irt="a@example.com", days=2)
    assert reply.conversation_id == a.conversation_id  # earliest holder of the id wins
    assert d.conversation_id != a.conversation_id
    assert session.get(Conversation, a.conversation_id).message_count == 2


def test_other_source_is_isolated(session, src):
    other = Source(kind="ietf_mail", key="idr")
    session.add(other)
    session.flush()
    a = add(session, src, "a@example.com", "Same")
    b = add(session, other, "b@example.com", "Same", irt="a@example.com")
    assert a.conversation_id != b.conversation_id


def test_rss_item_not_assigned(session):
    s = Source(kind="rss", key="https://example.com/feed.xml")
    session.add(s)
    session.flush()
    it = add(session, s, None, "Post", assign=False)
    assert conv.assign(session, it) is None
    assert it.conversation_id is None
    assert session.scalars(select(Conversation)).all() == []


def test_assign_is_idempotent(session, src):
    a = add(session, src, "a@example.com", "Hello")
    cid = conv.assign(session, a)
    assert cid == a.conversation_id
    assert session.get(Conversation, cid).message_count == 1
    assert len(session.scalars(select(Item)).all()) == 1
