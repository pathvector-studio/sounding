"""RSS/Atom adapter + fetch job against httpx.MockTransport and a real Postgres. Synthetic feeds."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select

from sounding.config import Settings
from sounding.db import get_session_factory
from sounding.jobs import default_adapters, run_fetch
from sounding.models import Conversation, JobRun, Source
from sounding.models import Item as ItemRow
from sounding.profile import Feed, Ietf, Profile
from sounding.sources import rss as rss_mod
from sounding.sources.base import SourceError
from sounding.sources.rss import RssAdapter, user_agent

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
CONTACT = "contact-7f3a@example.net"
FEED = "https://example.com/feed.xml"

RSS2 = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Example</title><link>https://example.com/</link>
<item><title>First post</title><link>https://example.com/p/1</link><guid>guid-1</guid>
 <pubDate>Fri, 06 Nov 2026 09:00:00 GMT</pubDate><author>bob.writer@example.com</author>
 <description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description></item>
<item><title>Second post</title><link>https://example.com/p/2</link><guid>guid-2</guid>
 <pubDate>Sat, 07 Nov 2026 09:00:00 GMT</pubDate><description>Plain summary</description></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Example Atom</title><id>urn:feed</id>
<updated>2026-11-07T10:00:00Z</updated>
<entry><title>Atom entry</title><id>urn:entry:1</id><link href="https://example.com/a/1"/>
 <updated>2026-11-07T10:00:00Z</updated><author><name>Carol Author</name></author>
 <content type="html">&lt;p&gt;Atom &lt;i&gt;body&lt;/i&gt;&lt;/p&gt;</content></entry>
</feed>"""

NO_GUID = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Has link</title><link>https://example.com/l/1</link><description>a</description></item>
<item><title>Nothing but title</title><description>b</description><pubDate>Fri, 06 Nov 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""

HTML_ONLY = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>H</title><guid>h1</guid>
<description>&lt;div&gt;&lt;script&gt;evil()&lt;/script&gt;&lt;p&gt;One&lt;/p&gt;&lt;p&gt;Two &amp;amp; three&lt;/p&gt;&lt;/div&gt;</description></item>
</channel></rss>"""


def make_settings(**kw) -> Settings:
    base = dict(
        database_url="postgresql+psycopg://u:p@localhost:1/none",
        fetch_contact=CONTACT,
        identity_secret="unit-test-identity-secret-0123456789",
        profile_path="/nonexistent/profile.yaml",
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


class Feeds:
    """A MockTransport handler that records requests and serves a routing table."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get(str(request.url))
        if handler is None:
            return httpx.Response(404)
        return handler(request) if callable(handler) else handler


def ok(body: bytes, **headers) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"content-type": "application/xml", **headers})


class Env:
    def __init__(self, session, routes=None, feeds=(FEED,), **settings_kw):
        self.session = session
        self.settings = make_settings(**settings_kw)
        self.feeds = Feeds(routes if routes is not None else {FEED: ok(RSS2, etag='"v1"', **{"last-modified": "Sat, 07 Nov 2026 09:00:00 GMT"})})
        self.adapter = RssAdapter(self.settings, transport=httpx.MockTransport(self.feeds), clock=lambda: NOW)
        self.profile = Profile(feeds=[Feed(url=u) for u in feeds])

    def run(self, **kw):
        return run_fetch(
            get_session_factory(), self.settings, adapters={"rss": self.adapter}, profile=self.profile, now=NOW, **kw
        )

    def one(self):
        (out,) = self.run()
        return out

    def items(self):
        self.session.expire_all()
        return self.session.scalars(select(ItemRow).order_by(ItemRow.published_at, ItemRow.external_id)).all()

    def source(self, key=FEED) -> Source:
        self.session.expire_all()
        return self.session.scalars(select(Source).where(Source.key == key)).one()


@pytest.fixture
def env(session):
    return Env(session)


def test_rss2_items(env):
    out = env.one()
    assert out.status == "ok" and out.counts["fetched"] == 2 and out.counts["new"] == 2
    assert "ancestors" not in out.counts
    first, second = env.items()
    assert first.external_id == "guid-1" and first.url == "https://example.com/p/1"
    assert first.title == "First post" and first.body_text == "Hello world"
    assert first.published_at == datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
    assert first.author_name == "b…" and first.author_key is None
    assert first.conversation_id is None and first.fetched_via == "poll" and first.is_auto is False
    assert second.body_text == "Plain summary" and second.author_name is None
    assert env.session.scalar(select(func.count()).select_from(Conversation)) == 0
    src = env.source()
    assert src.cursor == {"etag": '"v1"', "last_modified": "Sat, 07 Nov 2026 09:00:00 GMT"}
    assert src.last_status == "ok" and src.consecutive_failures == 0 and src.last_fetched_at


def test_atom_entry(session):
    e = Env(session, {FEED: ok(ATOM)})
    assert e.one().counts["new"] == 1
    (item,) = e.items()
    assert item.external_id == "urn:entry:1" and item.url == "https://example.com/a/1"
    assert item.author_name == "Carol Author" and item.body_text == "Atom body"
    assert item.published_at == datetime(2026, 11, 7, 10, 0, tzinfo=UTC)
    assert e.source().cursor == {"etag": None, "last_modified": None}


def test_conditional_get_304_keeps_cursor(env):
    env.one()
    cursor = env.source().cursor

    def handler(request):
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return ok(RSS2)

    env.feeds.routes[FEED] = handler
    out = env.one()
    sent = env.feeds.requests[-1].headers
    assert sent["if-none-match"] == '"v1"' and sent["if-modified-since"] == "Sat, 07 Nov 2026 09:00:00 GMT"
    assert out.status == "ok" and out.counts["fetched"] == 0 and out.counts["new"] == 0
    assert env.source().cursor == cursor
    assert len(env.items()) == 2


def test_idempotent_second_fetch(env):
    assert env.one().counts["new"] == 2
    out = env.one()  # same body, no 304
    assert out.counts["fetched"] == 2 and out.counts["new"] == 0 and out.counts["duplicates"] == 2
    assert len(env.items()) == 2


def redirect_chain(n: int, final: str = FEED):
    """n redirects starting at FEED-0, ending at a 200 feed."""
    routes = {}
    urls = [f"https://example.com/r{i}" for i in range(n)]
    start = urls[0] if n else final
    for i, u in enumerate(urls):
        nxt = urls[i + 1] if i + 1 < n else final
        routes[u] = httpx.Response(302, headers={"location": nxt})
    routes[final] = ok(RSS2)
    return start, routes


def test_redirects_up_to_five_ok(session):
    start, routes = redirect_chain(5)
    e = Env(session, routes, feeds=(start,))
    assert e.one().status == "ok"
    assert len(e.feeds.requests) == 6


def test_six_redirects_rejected(session):
    start, routes = redirect_chain(6)
    e = Env(session, routes, feeds=(start,))
    out = e.one()
    assert out.status == "failed" and out.error == "too_many_redirects"
    assert e.items() == []


def test_relative_redirect_is_resolved(session):
    e = Env(session, {
        "https://example.com/old": httpx.Response(301, headers={"location": "/feed.xml"}),
        FEED: ok(RSS2),
    }, feeds=("https://example.com/old",))
    assert e.one().status == "ok"


@pytest.mark.parametrize("target", ["file:///etc/passwd", "ftp://example.com/feed", "javascript:alert(1)"])
def test_redirect_to_other_scheme_rejected(session, target):
    e = Env(session, {FEED: httpx.Response(302, headers={"location": target})})
    out = e.one()
    assert out.status == "failed" and out.error == "unsupported_url"
    assert len(e.feeds.requests) == 1


def test_initial_non_http_url_rejected(session):
    e = Env(session, {})
    with pytest.raises(SourceError) as exc:
        e.adapter.fetch(Source(kind="rss", key="file:///etc/passwd"), None)
    assert exc.value.code == "unsupported_url" and not e.feeds.requests


def test_oversized_body_rejected(session, monkeypatch):
    monkeypatch.setattr(rss_mod, "MAX_BODY_BYTES", 1024)
    big = RSS2.replace(b"</channel>", b"<!--" + b"x" * 4096 + b"--></channel>")
    e = Env(session, {FEED: ok(big)})
    out = e.one()
    assert out.status == "failed" and out.error == "response_too_large"
    assert e.items() == []


def test_oversized_declared_length_rejected(session, monkeypatch):
    monkeypatch.setattr(rss_mod, "MAX_BODY_BYTES", 1024)

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("body must not be read")

    e = Env(session, {FEED: httpx.Response(200, headers={"content-length": "999999"}, stream=Stream())})
    assert e.one().error == "response_too_large"


def test_oversized_streaming_stops_early(session, monkeypatch):
    monkeypatch.setattr(rss_mod, "MAX_BODY_BYTES", 1024)
    read = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            for i in range(1000):
                read.append(i)
                yield b"y" * 512

    e = Env(session, {FEED: httpx.Response(200, stream=Stream())})
    assert e.one().error == "response_too_large"
    assert len(read) < 10


@pytest.mark.parametrize("status", [404, 410, 500, 503])
def test_http_errors_use_fixed_codes(session, status):
    e = Env(session, {FEED: httpx.Response(status, content=b"secret server text")})
    out = e.one()
    assert out.status == "failed" and out.error == f"http_{status}"
    assert "secret" not in (e.source().last_error or "")
    assert e.source().consecutive_failures == 1


def test_timeout_and_connection_errors(session):
    def boom(request):
        raise httpx.ConnectTimeout("t", request=request)

    e = Env(session, {FEED: boom})
    assert e.one().error == "timeout"

    def refuse(request):
        raise httpx.ConnectError("c", request=request)

    e.feeds.routes[FEED] = refuse
    assert e.one().error == "connection_error"


def test_broken_xml_is_parse_error(session):
    for body in (b"<rss version='2.0'><chan", b"<html><body>Not a feed</body></html>", b""):
        e = Env(session, {FEED: ok(body)})
        out = e.one()
        assert out.status == "failed" and out.error == "parse_error", body
        assert e.items() == []


def test_bozo_with_entries_proceeds(session):
    body = RSS2.replace(b"</channel></rss>", b"</channel>")  # unclosed root: malformed but parseable
    e = Env(session, {FEED: ok(body)})
    out = e.one()
    assert out.status == "ok" and out.counts["new"] == 2 and out.counts["notes"] == ["parse_warning"]


def test_empty_valid_feed_is_ok(session):
    e = Env(session, {FEED: ok(b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>')})
    out = e.one()
    assert out.status == "ok" and out.counts["fetched"] == 0


def test_entries_without_guid(session):
    e = Env(session, {FEED: ok(NO_GUID)})
    assert e.one().counts["new"] == 2
    by_title = {i.title: i for i in e.items()}
    assert by_title["Has link"].external_id == "https://example.com/l/1"
    hashed = by_title["Nothing but title"]
    assert hashed.external_id.startswith("hash:") and hashed.url is None
    assert hashed.published_at == datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
    assert by_title["Has link"].published_at == NOW  # no date: fetch time
    assert e.one().counts["new"] == 0  # ids are stable across fetches
    assert len(e.items()) == 2


def test_html_only_content_becomes_text(session):
    e = Env(session, {FEED: ok(HTML_ONLY)})
    e.one()
    (item,) = e.items()
    assert item.body_text == "One\n\nTwo & three" and "evil" not in item.body_text


def test_non_http_entry_link_not_stored_as_url(session):
    body = NO_GUID.replace(b"https://example.com/l/1", b"javascript:alert(1)")
    e = Env(session, {FEED: ok(body)})
    e.one()
    assert all(i.url is None for i in e.items())


def test_rss_runs_without_contact_while_ietf_is_skipped(session):
    from fake_imap import FakeServer  # noqa: PLC0415 - sibling test helper
    from sounding.sources.ietf_mail import IetfMailAdapter

    settings = make_settings(fetch_contact="change-me")
    server = FakeServer()
    server.add_list("sidrops")
    ietf = IetfMailAdapter(settings, client_factory=server.factory, sleep=lambda s: None, clock=lambda: NOW)
    feeds = Feeds({FEED: ok(RSS2)})
    rss = RssAdapter(settings, transport=httpx.MockTransport(feeds), clock=lambda: NOW)
    profile = Profile(ietf=Ietf(working_groups=["sidrops"]), feeds=[Feed(url=FEED)])
    outs = run_fetch(
        get_session_factory(), settings, adapters={"ietf_mail": ietf, "rss": rss}, profile=profile, now=NOW
    )
    assert [(o.kind, o.status) for o in outs] == [("ietf_mail", "skipped"), ("rss", "ok")]
    assert outs[0].error == "fetch_contact_not_set" and outs[1].counts["new"] == 2
    assert not server.commands
    assert feeds.requests[0].headers["user-agent"].endswith("sounding)")


def test_default_adapters_registers_rss_after_ietf():
    adapters = default_adapters(make_settings())
    try:
        assert list(adapters) == ["ietf_mail", "rss"]
        assert adapters["rss"].kind == "rss" and not hasattr(adapters["rss"], "fetch_by_ids")
    finally:
        for a in adapters.values():
            getattr(a, "close", lambda: None)()


def test_user_agent_with_and_without_contact(monkeypatch):
    monkeypatch.setattr(rss_mod, "app_version", lambda: "1.2.3")
    repo = "https://github.com/pathvector-studio/sounding"
    assert user_agent(make_settings()) == f"Sounding/1.2.3 (+{repo}; {CONTACT})"
    for contact in ("", "   ", "change-me", "change-me-please"):
        assert user_agent(make_settings(fetch_contact=contact)) == f"Sounding/1.2.3 (+{repo})"


def test_user_agent_is_sent_on_requests(env):
    env.one()
    ua = env.feeds.requests[0].headers["user-agent"]
    assert ua.startswith("Sounding/") and CONTACT in ua and "pathvector-studio/sounding" in ua


def test_app_version_sources(monkeypatch):
    monkeypatch.setenv("SOUNDING_VERSION", "9.9.9")
    assert rss_mod.app_version() == "9.9.9"
    monkeypatch.delenv("SOUNDING_VERSION")
    assert rss_mod.app_version()  # VERSION file in the checkout, else "dev"


def test_contact_never_logged(session, caplog):
    caplog.set_level(logging.DEBUG)
    # success, http failure, redirect failure and parse failure all log (or not) without the contact
    for routes in (
        {FEED: ok(RSS2)},
        {FEED: httpx.Response(500)},
        {FEED: httpx.Response(302, headers={"location": "file:///x"})},
        {FEED: ok(b"garbage")},
    ):
        for _ in range(3):  # three failures in a row also trigger the consecutive-failures warning
            Env(session, routes).one()
    assert caplog.records
    text = caplog.text + "\n".join(str(r.args) for r in caplog.records)
    assert CONTACT not in text and "contact-7f3a" not in text
    jobs = session.scalars(select(JobRun)).all()
    assert all(CONTACT not in (j.error or "") and CONTACT not in str(j.counts) for j in jobs)


def test_failed_source_does_not_stop_others(session):
    e = Env(session, {FEED: httpx.Response(500), "https://example.com/b.xml": ok(ATOM)},
            feeds=(FEED, "https://example.com/b.xml"))
    outs = e.run()
    assert sorted(o.status for o in outs) == ["failed", "ok"]


def test_rss_runs_without_identity_secret_while_ietf_is_skipped(session):
    from fake_imap import FakeServer  # noqa: PLC0415 - sibling test helper
    from sounding.sources.ietf_mail import IetfMailAdapter

    settings = make_settings(identity_secret="")
    server = FakeServer()
    server.add_list("sidrops")
    ietf = IetfMailAdapter(settings, client_factory=server.factory, sleep=lambda s: None, clock=lambda: NOW)
    rss = RssAdapter(settings, transport=httpx.MockTransport(Feeds({FEED: ok(RSS2)})), clock=lambda: NOW)
    profile = Profile(ietf=Ietf(working_groups=["sidrops"]), feeds=[Feed(url=FEED)])
    outs = run_fetch(
        get_session_factory(), settings, adapters={"ietf_mail": ietf, "rss": rss}, profile=profile, now=NOW
    )
    assert [(o.kind, o.status, o.error) for o in outs] == [
        ("ietf_mail", "skipped", "identity_secret_not_set"), ("rss", "ok", None),
    ]
    assert not server.commands
