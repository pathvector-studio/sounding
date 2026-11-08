from datetime import UTC, datetime
from email.message import EmailMessage

import pytest
from sqlalchemy import select

from sounding.classify import EventSpec, classify_auto, draft_ref, event_for, record_event
from sounding.models import Event, Item, Source


def msg(frm="Alice Example <alice@example.com>", subject="Hello", **headers):
    m = EmailMessage()
    m["From"] = frm
    m["Subject"] = subject
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    return m


def test_human_is_not_auto():
    assert classify_auto(msg()) is None
    assert classify_auto(msg(Auto_Submitted="no")) is None
    assert classify_auto(msg(subject="Re: I-D Action: draft-x-y-00.txt")) is None


@pytest.mark.parametrize(
    "m,reason",
    [
        (msg(Auto_Submitted="auto-generated"), "auto_submitted_header"),
        (msg(Precedence="bulk"), "precedence_bulk"),
        (msg(Precedence="Junk"), "precedence_bulk"),
        (msg(X_Auto_Response_Suppress="All"), "auto_response_suppress"),
        (msg(frm="internet-drafts@example.org"), "bot_sender"),
        (msg(frm="IETF Datatracker <noreply@example.org>"), "bot_sender"),
        (msg(subject="[sidrops] I-D Action: draft-example-foo-01.txt"), "bot_subject"),
        (msg(subject="Last Call: <draft-example-foo-05.txt> (Title) to Proposed Standard"), "bot_subject"),
    ],
)
def test_auto_reasons(m, reason):
    assert classify_auto(m) == reason


class FakeItem:
    def __init__(self, body=None):
        self.body_text = body


@pytest.mark.parametrize(
    "subject,kind,ref",
    [
        ("I-D Action: draft-ietf-sidrops-aspa-verification-19.txt", "ietf.id_action", "draft-ietf-sidrops-aspa-verification-19"),
        ("[sidrops] I-D Action: draft-Example-Foo-00.txt", "ietf.id_action", "draft-example-foo-00"),
        ("Call for adoption: draft-example-foo-02", "ietf.wg_adoption_call", "draft-example-foo-02"),
        ("[wg] WG adoption call for draft-example-bar", "ietf.wg_adoption_call", "draft-example-bar"),
        ("WG Last Call: draft-example-foo-03", "ietf.last_call", "draft-example-foo-03"),
        ("WGLC for draft-example-foo-03", "ietf.last_call", "draft-example-foo-03"),
        ("Last Call: <draft-example-foo-05.txt> (Title) to Proposed Standard", "ietf.last_call", "draft-example-foo-05"),
        ("RFC 9999 on Some Protocol", "ietf.publication", None),
        ("Protocol Action: 'Title' to Proposed Standard (draft-example-foo-06.txt)", "ietf.publication", "draft-example-foo-06"),
        ("Document Action: 'Title' to Informational RFC", "ietf.publication", None),
        ("Meeting minutes posted", "ietf.other_notice", None),
    ],
)
def test_event_kinds(subject, kind, ref):
    spec = event_for(FakeItem(), subject)
    assert spec == EventSpec(kind, ref)


def test_event_empty_subject_and_body_ref():
    assert event_for(FakeItem(), "  ") is None
    body = "\n" * 30 + "draft-too-far-00"
    assert event_for(FakeItem(body), "Notice").ref is None
    assert event_for(FakeItem("Hi,\nSee Draft-Example-Baz-07 for details"), "Notice").ref == "draft-example-baz-07"


def test_draft_ref():
    assert draft_ref(None, "") is None
    assert draft_ref("x draft-a-b-00.txt y") == "draft-a-b-00"
    assert draft_ref("no ref", "draft-c-d") == "draft-c-d"


def test_record_event_idempotent(session):
    s = Source(kind="ietf_mail", key="sidrops")
    session.add(s)
    session.flush()
    it = Item(source_id=s.id, external_id="1", title="t", published_at=datetime(2026, 10, 1, tzinfo=UTC), message_id="a@example.com")
    session.add(it)
    session.flush()
    spec = event_for(it, "I-D Action: draft-example-foo-01.txt")
    assert record_event(session, it, spec) is True
    assert record_event(session, it, spec) is False
    evs = session.scalars(select(Event)).all()
    assert len(evs) == 1
    e = evs[0]
    assert (e.kind, e.ref, e.item_id, e.source_id, e.occurred_at) == ("ietf.id_action", "draft-example-foo-01", it.id, s.id, it.published_at)
    # a different kind for the same item is allowed by the constraint
    assert record_event(session, it, EventSpec("ietf.other_notice", None)) is True
