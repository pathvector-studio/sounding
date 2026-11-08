"""IETF mail adapter + fetch job against a fake IMAP server and a real Postgres."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from fake_imap import FakeServer, make_mail
from sqlalchemy import func, inspect, select, text

from sounding.config import Settings
from sounding.db import get_engine, get_session_factory
from sounding.jobs import run_fetch
from sounding.models import Base, Conversation, Event, JobRun, Source
from sounding.models import Item as ItemRow
from sounding.profile import Ietf, Profile
from sounding.sources.ietf_mail import IetfMailAdapter, parse_message

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
CONTACT = "contact-7f3a@example.net"
SECRET = "unit-test-identity-secret-0123456789"
SENDER_ADDR = "alice.sender@example.com"


def days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


def make_settings(**kw) -> Settings:
    base = dict(
        database_url="postgresql+psycopg://u:p@localhost:1/none",
        fetch_contact=CONTACT,
        identity_secret=SECRET,
        ietf_backfill_days=30,
        ietf_max_messages_per_run=500,
        ietf_ancestor_max_depth=50,
        ietf_ancestor_max_age_days=365,
        profile_path="/nonexistent/profile.yaml",
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


class Env:
    def __init__(self, session, lists=("sidrops",), **settings_kw):
        self.session = session
        self.settings = make_settings(**settings_kw)
        self.server = FakeServer()
        self.sleeps: list[float] = []
        self.boxes = {name: self.server.add_list(name) for name in lists}
        self.profile = Profile(ietf=Ietf(working_groups=list(lists)))
        self.adapter = IetfMailAdapter(
            self.settings, client_factory=self.server.factory, sleep=self.sleeps.append, clock=lambda: NOW
        )

    def add(self, msgid, subject, when, list_name="sidrops", **kw) -> int:
        return self.boxes[list_name].add(make_mail(msgid, subject, when, **kw), when)

    def run(self, only=None):
        return run_fetch(
            get_session_factory(), self.settings, adapters={"ietf_mail": self.adapter},
            profile=self.profile, only_source=only, now=NOW,
        )

    def one(self, only="sidrops"):
        (out,) = self.run(only)
        return out

    def count(self, model) -> int:
        self.session.expire_all()
        return self.session.scalar(select(func.count()).select_from(model))

    def source(self, key="sidrops") -> Source:
        self.session.expire_all()
        return self.session.scalars(select(Source).where(Source.key == key)).one()


@pytest.fixture
def env(session):
    return Env(session)


def test_first_run_uses_since_examine_and_stores(env):
    env.add("a1@example.com", "[sidrops] Topic", days_ago(2), archived_at="https://archive.example.org/arch/msg/sidrops/x1")
    env.add("a2@example.com", "Re: [sidrops] Topic", days_ago(1), in_reply_to="a1@example.com")
    env.add("old@example.com", "Ancient", days_ago(60))
    out = env.one()
    assert out.status == "ok" and out.counts["new"] == 2
    assert env.server.cmds("EXAMINE") == ['EXAMINE "Shared Folders/sidrops"']
    assert not env.server.cmds("SELECT")
    assert env.server.passwords == [CONTACT]
    assert "UID SEARCH SINCE 09-Oct-2026" in env.server.commands
    rows = env.session.scalars(select(ItemRow).order_by(ItemRow.published_at)).all()
    assert [r.external_id for r in rows] == ["a1@example.com", "a2@example.com"]
    assert rows[0].url == "https://archive.example.org/arch/msg/sidrops/x1"
    assert rows[0].author_name == "Alice Example" and rows[0].author_key.startswith("mail:")
    assert rows[0].body_hash and rows[0].fetched_via == "poll"
    assert rows[0].conversation_id == rows[1].conversation_id
    src = env.source()
    assert src.cursor["last_uid"] == 2 and src.cursor["uidvalidity"] == 1000
    assert src.last_status == "ok" and src.consecutive_failures == 0 and src.last_fetched_at
    job = env.session.scalars(select(JobRun)).one()
    assert job.status == "ok" and job.counts["new"] == 2


def test_incremental_and_last_message_edge(env):
    env.add("a1@example.com", "One", days_ago(3))
    env.add("a2@example.com", "Two", days_ago(2))
    assert env.one().counts["new"] == 2
    env.add("a3@example.com", "Three", days_ago(1))
    out = env.one()
    assert out.counts["new"] == 1 and out.counts["fetched"] == 1
    assert "UID SEARCH UID 3:*" in env.server.commands
    # nothing new: the server answers "4:*" with the last message (uid 3); it must be ignored
    out = env.one()
    assert out.counts["fetched"] == 0 and out.counts["new"] == 0
    assert "UID SEARCH UID 4:*" in env.server.commands
    assert env.source().cursor["last_uid"] == 3
    assert env.count(ItemRow) == 3


def test_empty_first_run_does_not_fetch_whole_mailbox_later(env):
    env.add("old@example.com", "Ancient", days_ago(90))
    assert env.one().counts["fetched"] == 0
    assert env.source().cursor["last_uid"] == 1  # UIDNEXT - 1
    env.add("new@example.com", "Fresh", days_ago(1))
    out = env.one()
    assert out.counts["new"] == 1 and "UID SEARCH UID 2:*" in env.server.commands


def test_uidvalidity_change_refetches_without_duplicates(env):
    env.add("a1@example.com", "One", days_ago(3))
    env.add("a2@example.com", "Two", days_ago(2))
    env.one()
    box = env.boxes["sidrops"]
    box.uidvalidity = 2000  # the server renumbered the mailbox
    box.messages = {10: box.messages[1], 11: box.messages[2]}
    box.next_uid = 12
    out = env.one()
    assert "uidvalidity_changed" in out.counts["notes"]
    assert out.counts["fetched"] == 2 and out.counts["new"] == 0 and out.counts["duplicates"] == 2
    assert env.source().cursor == {"uidvalidity": 2000, "last_uid": 11, "missing_ids": []}
    assert env.count(ItemRow) == 2


def test_per_run_cap_and_batch_pause(session):
    env = Env(session, ietf_max_messages_per_run=120)
    for n in range(130):
        env.add(f"m{n}@example.com", f"Topic {n}", days_ago(5) + timedelta(minutes=n))
    out = env.one()
    assert out.counts["new"] == 120 and "truncated" in out.counts["notes"]
    assert env.sleeps == [1.0, 1.0]  # 3 batches (50, 50, 20) -> 2 pauses; nothing else sleeps
    out = env.one()
    assert out.counts["new"] == 10 and "notes" not in out.counts
    assert env.count(ItemRow) == 130


def test_login_failure_retries_once_after_30s(env):
    env.add("a1@example.com", "One", days_ago(1))
    env.server.login_failures = 1
    out = env.one()
    assert out.status == "ok" and env.sleeps == [30.0]
    assert env.server.cmds("LOGIN") == ["LOGIN anonymous"] * 2


def test_connection_failure_twice_fails_the_source_only(session):
    env = Env(session, lists=("sidrops", "grow"))
    env.add("s1@example.com", "Sidrops topic", days_ago(1))
    env.server.connect_failures = 2
    outs = env.run()
    by_key = {o.key: o for o in outs}
    # sources run ordered by key; "grow" burns both attempts, "sidrops" then connects fine
    assert by_key["grow"].status == "failed" and by_key["grow"].error == "connect_failed"
    assert by_key["sidrops"].status == "ok" and by_key["sidrops"].counts["new"] == 1
    assert env.sleeps == [30.0]
    src = env.source("grow")
    assert src.consecutive_failures == 1 and src.last_status == "failed" and src.last_error == "connect_failed"
    failed = env.session.scalars(select(JobRun).where(JobRun.status == "failed")).one()
    assert failed.error == "connect_failed"


def test_three_consecutive_failures_leave_a_warning(env):
    for expected in (1, 2, 3):
        env.server.connect_failures = 2
        out = env.one()
        assert out.status == "failed" and env.source().consecutive_failures == expected
    last = env.session.scalars(select(JobRun).order_by(JobRun.id.desc())).first()
    assert last.counts["warning"] == "consecutive_failures"
    env.add("a1@example.com", "One", days_ago(1))
    assert env.one().status == "ok" and env.source().consecutive_failures == 0


def test_mid_run_disconnect_keeps_progress_and_resumes(env):
    for n in range(120):
        env.add(f"m{n}@example.com", f"Topic {n}", days_ago(5) + timedelta(minutes=n))
    env.server.disconnect_on_fetch = 2
    out = env.one()
    assert out.status == "failed" and out.error == "disconnected"
    assert out.counts["new"] == 50
    src = env.source()
    assert src.cursor["last_uid"] == 50 and src.consecutive_failures == 1
    out = env.one()  # reconnects and continues from uid 51
    assert out.status == "ok" and out.counts["new"] == 70
    assert env.server.connections == 2 and env.count(ItemRow) == 120


def test_mailbox_not_found_does_not_stop_other_lists(session):
    env = Env(session, lists=("sidrops", "grow"))
    env.profile = Profile(ietf=Ietf(working_groups=["nosuchlist", "grow"]))
    env.add("g1@example.com", "Grow topic", days_ago(1), list_name="grow")
    outs = {o.key: o for o in env.run()}
    assert outs["nosuchlist"].status == "failed" and outs["nosuchlist"].error == "mailbox_not_found"
    assert outs["grow"].status == "ok"
    assert env.source("nosuchlist").last_status == "not_found"


def test_fetching_twice_is_idempotent(env):
    env.add("a1@example.com", "I-D Action: draft-example-foo-01.txt", days_ago(3), auto=True)
    env.add("a2@example.com", "Topic", days_ago(2))
    env.add("a3@example.com", "Re: Topic", days_ago(1), in_reply_to="a2@example.com")
    first = env.one()
    before = (env.count(ItemRow), env.count(Conversation), env.count(Event))
    assert first.counts["new"] == 3
    env.source().cursor = None
    env.session.commit()
    second = env.one()
    assert second.counts["new"] == 0 and second.counts["duplicates"] == 3
    assert (env.count(ItemRow), env.count(Conversation), env.count(Event)) == before == (3, 2, 1)


def test_auto_mail_becomes_an_event(env):
    env.add("n1@example.com", "I-D Action: draft-example-foo-01.txt", days_ago(1), auto=True,
            sender="Datatracker <internet-drafts@example.org>", body="A new version of draft-example-foo-01.")
    out = env.one()
    assert out.counts["events"] == 1
    item = env.session.scalars(select(ItemRow)).one()
    assert item.is_auto and item.auto_reason == "auto_submitted_header"
    ev = env.session.scalars(select(Event)).one()
    assert ev.kind == "ietf.id_action" and ev.ref == "draft-example-foo-01" and ev.item_id == item.id


def test_fetch_contact_not_set_skips_without_connecting(session):
    for contact in ("change-me@example.com", ""):
        env = Env(session, fetch_contact=contact)
        env.add("a1@example.com", "One", days_ago(1))
        out = env.one()
        assert out.status == "skipped" and out.error == "fetch_contact_not_set"
        assert env.server.connections == 0 and not env.server.commands
        job = env.session.scalars(select(JobRun).order_by(JobRun.id.desc())).first()
        assert job.status == "skipped" and job.error == "fetch_contact_not_set"
        assert env.count(ItemRow) == 0


def test_html_only_plain_preferred_and_attachments_ignored():
    kw = dict(uid=1, uidvalidity=7, secret=SECRET, fallback_date=NOW)
    html = parse_message(
        make_mail("h@example.com", "H", NOW, html_only=True,
                  html="<html><head><title>x</title><style>p{}</style></head><body><p>Hello &amp; <b>welcome</b></p>"
                       "<script>evil()</script><div>second<br>line</div></body></html>"),
        **kw,
    )
    assert html.body_text == "Hello & welcome\n\nsecond\nline"
    both = parse_message(make_mail("b@example.com", "B", NOW, body="plain wins", html="<p>html loses</p>"), **kw)
    assert both.body_text == "plain wins"
    att = parse_message(make_mail("c@example.com", "C", NOW, body="text", attachment=True), **kw)
    assert att.body_text == "text" and "attached-bytes" not in att.body_text
    bad = parse_message(make_mail("d@example.com", "D", NOW, body="café", charset="utf-8").replace(b"utf-8", b"x-nonexistent"), **kw)
    assert bad.body_text  # decode errors never raise
    noid = parse_message(make_mail(None, "No id", NOW), **kw)
    assert noid.external_id == "uid:7:1" and noid.conversation_hints.message_id is None


def test_message_headers_become_hints_and_url():
    it = parse_message(
        make_mail("Child@Example.com", "Re: X", NOW, in_reply_to="Parent@Example.com",
                  references=["Root@Example.com", "Parent@Example.com"], archived_at="https://archive.example.org/m/1"),
        uid=1, uidvalidity=7, secret=SECRET, fallback_date=NOW,
    )
    assert it.external_id == "child@example.com"
    h = it.conversation_hints
    assert (h.message_id, h.in_reply_to, h.references) == (
        "child@example.com", "parent@example.com", ("root@example.com", "parent@example.com"))
    assert it.url == "https://archive.example.org/m/1" and it.author_name == "Alice Example"
    assert SENDER_ADDR not in repr(it)


# -- ancestor completion ---------------------------------------------------------------------


def test_ancestors_outside_the_window_are_completed(env):
    env.add("root@example.com", "Big topic", days_ago(100))
    env.add("mid@example.com", "Re: Big topic", days_ago(45), in_reply_to="root@example.com",
            references=["root@example.com"])
    env.add("leaf@example.com", "Re: Big topic", days_ago(1), in_reply_to="mid@example.com",
            references=["root@example.com", "mid@example.com"])
    out = env.one()
    assert out.counts["new"] == 1 and out.counts["ancestors"] == 2
    rows = {r.external_id: r for r in env.session.scalars(select(ItemRow))}
    assert rows["leaf@example.com"].fetched_via == "poll"
    assert rows["mid@example.com"].fetched_via == rows["root@example.com"].fetched_via == "ancestor"
    conv = env.session.scalars(select(Conversation).where(Conversation.merged_into.is_(None))).one()
    assert conv.message_count == 3 and conv.subject == "Big topic"
    assert any(c.startswith("UID SEARCH HEADER Message-ID") for c in env.server.commands)


def test_ancestor_depth_limit(session):
    env = Env(session, ietf_ancestor_max_depth=2)
    refs = [f"p{n}@example.com" for n in range(5)]  # p0 is the oldest
    for n, mid in enumerate(refs):
        env.add(mid, "Chain", days_ago(100 - n), in_reply_to=refs[n - 1] if n else None, references=refs[:n] or None)
    env.add("leaf@example.com", "Re: Chain", days_ago(1), in_reply_to="p4@example.com", references=refs)
    out = env.one()
    assert out.counts["ancestors"] == 2 and out.counts["ancestor_lookups"] == 2
    assert env.count(ItemRow) == 3
    out = env.one()  # the next run does not continue on its own (no new polled messages to start from)
    assert env.count(ItemRow) == 3 and out.counts["ancestors"] == 0


def test_ancestor_age_limit_and_missing_ids(session):
    env = Env(session, ietf_ancestor_max_age_days=365)
    env.add("ancient@example.com", "Very old", days_ago(400))
    env.add("old@example.com", "Re: Very old", days_ago(100), in_reply_to="ancient@example.com")
    env.add("leaf@example.com", "Re: Very old", days_ago(1), in_reply_to="old@example.com",
            references=["ancient@example.com", "old@example.com", "elsewhere@example.net"])
    out = env.one()
    assert out.counts["ancestors"] == 1 and out.counts["ancestors_skipped_old"] == 1
    assert env.count(ItemRow) == 2  # leaf + old; "ancient" was too old
    missing = env.source().cursor["missing_ids"]
    assert sorted(missing) == ["ancient@example.com", "elsewhere@example.net"]
    searches = len(env.server.cmds("UID SEARCH HEADER"))
    env.add("leaf2@example.com", "Re: Very old", days_ago(0.5), in_reply_to="leaf@example.com",
            references=["ancient@example.com", "old@example.com", "elsewhere@example.net", "leaf@example.com"])
    out = env.one()
    assert out.counts["ancestor_lookups"] == 0
    assert len(env.server.cmds("UID SEARCH HEADER")) == searches  # remembered ids are not searched again
    assert sorted(env.source().cursor["missing_ids"]) == ["ancient@example.com", "elsewhere@example.net"]


def test_ancestor_lookup_failure_is_not_recorded_as_missing(env):
    env.add("root@example.com", "Topic", days_ago(100))
    env.add("leaf@example.com", "Re: Topic", days_ago(1), in_reply_to="root@example.com")
    real_uid = type(env.server).factory

    def flaky_factory(host, port, timeout):
        client = real_uid(env.server, host, port, timeout)
        real = client.uid

        def uid(command, *args):
            if command == "SEARCH" and args[0] == "HEADER":
                raise OSError("dropped")
            return real(command, *args)

        client.uid = uid
        return client

    env.adapter = IetfMailAdapter(env.settings, client_factory=flaky_factory, sleep=env.sleeps.append, clock=lambda: NOW)
    out = env.one()
    assert out.status == "ok" and out.counts["new"] == 1 and out.counts["ancestor_error"] == "imap_error"
    assert env.source().cursor["missing_ids"] == []


# -- privacy ---------------------------------------------------------------------------------


def _scan_for(session, needle: str) -> int:
    insp = inspect(get_engine())
    checked = 0
    for table in Base.metadata.sorted_tables:
        assert insp.has_table(table.name)
        for col in table.columns:
            hit = session.execute(
                text(f'SELECT count(*) FROM "{table.name}" WHERE lower("{col.name}"::text) LIKE :p'),
                {"p": f"%{needle.lower()}%"},
            ).scalar_one()
            assert hit == 0, f"{needle} found in {table.name}.{col.name}"
            checked += 1
    return checked


def test_address_is_in_no_column_after_adapter_ingestion(env):
    env.add("a1@example.com", "Topic", days_ago(3), body="No address here.")
    env.add("a2@example.com", "Re: Topic", days_ago(2), in_reply_to="a1@example.com",
            sender=f"{SENDER_ADDR.upper()}")
    env.add("a3@example.com", "I-D Action: draft-example-foo-01.txt", days_ago(1), auto=True,
            sender=f"Bot <{SENDER_ADDR}>")
    env.one()
    env.session.expire_all()
    assert _scan_for(env.session, SENDER_ADDR) > 30
    assert env.session.scalar(text("SELECT count(DISTINCT author_key) FROM items WHERE author_key IS NOT NULL")) == 1
    assert _scan_for(env.session, CONTACT) > 30  # the contact is not stored either
    assert _scan_for(env.session, SECRET) > 30


def test_secrets_and_addresses_never_reach_logs_or_errors(session, caplog):
    caplog.set_level(logging.DEBUG)
    env = Env(session, lists=("sidrops", "grow"))
    env.profile = Profile(ietf=Ietf(working_groups=["sidrops", "grow", "nosuchlist"]))
    env.add("a1@example.com", "Topic", days_ago(2))
    env.add("g1@example.com", "Other", days_ago(2), list_name="grow", body=f"write to {SENDER_ADDR}")
    env.server.login_failures = 1  # the fake echoes the password in its error text
    env.run()
    env.server.connect_failures = 2
    env.source().cursor = None
    env.run("sidrops")
    env.server.disconnect_on_fetch = env.server.fetch_calls + 1
    env.add("a2@example.com", "Later", days_ago(1))
    env.run("sidrops")
    blob = caplog.text
    for secret in (CONTACT, SECRET, SENDER_ADDR, "alice.sender"):
        assert secret not in blob
    session.expire_all()
    stored = " ".join(filter(None, [*session.scalars(select(Source.last_error)), *session.scalars(select(JobRun.error)),
                                    *(str(c) for c in session.scalars(select(JobRun.counts)))]))
    for secret in (CONTACT, SECRET, SENDER_ADDR):
        assert secret not in stored
    assert caplog.records  # something was logged (the test is not vacuous)
