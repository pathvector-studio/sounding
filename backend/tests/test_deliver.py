"""Delivery (spec phase 3, R10/R11): email through a local fake SMTP server, webhook through httpx.MockTransport.

Real Postgres. Nothing here connects to an external host.
"""

from __future__ import annotations

import base64
import email
import email.policy
import json
import logging
import smtplib
import socketserver
import ssl
import threading
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select, text
from test_digest import END, NOW, add_state, settings as base_settings
from test_select import mksrc, msg, prof

from sounding import deliver, jobs
from sounding.cli import format_status, main
from sounding.config import get_settings
from sounding.db import get_engine, get_session_factory
from sounding.deliver import DeliveryError, build_email, build_webhook_message, deliver_digest, webhook_flavor, webhook_payload
from sounding.models import Delivery, Digest, JobRun

WEBHOOK = "https://example.com/webhook/ZZHOOKTOKEN-8f3a"
DISCORD = "https://discord.com/api/webhooks/123/ZZDISCORDTOKEN"
SLACK = "https://hooks.slack.com/services/dummy/dummy/ZZSLACK"
SMTP_USER = "zz-smtp-user"
SMTP_PASSWORD = "zz-smtp-password-4471"
TO = "you@example.com"
FROM = "sounding@example.com"
SECRETS = [WEBHOOK, "ZZHOOKTOKEN", SMTP_USER, SMTP_PASSWORD, TO, FROM]
BODY_MARKER = "ZZBODYMARKER the body of the message"


@pytest.fixture
def sf(session):
    return get_session_factory()


def cfg(**kw):
    base = dict(
        delivery_channels="web,email,webhook,atom", webhook_url=WEBHOOK, smtp_host="smtp.example.com", smtp_port=587,
        smtp_user=SMTP_USER, smtp_password=SMTP_PASSWORD, smtp_from=FROM, digest_to=TO,
    )
    base.update(kw)
    return base_settings(**base)


# --- a tiny SMTP server (stdlib only) -----------------------------------------------------------------

class FakeSMTP:
    """Speaks EHLO, AUTH PLAIN, MAIL, RCPT, DATA, QUIT. ``starttls`` decides whether EHLO advertises STARTTLS."""

    def __init__(self, *, starttls: bool = True, auth_ok: bool = True, reject_rcpt: bool = False) -> None:
        self.messages: list[bytes] = []
        self.auth: list[str] = []
        self.rcpts: list[str] = []
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def reply(self, line: str) -> None:
                self.wfile.write(line.encode() + b"\r\n")

            def handle(self) -> None:
                self.reply("220 fake ESMTP")
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    line = raw.decode(errors="replace").rstrip("\r\n")
                    cmd = line.split(" ", 1)[0].upper()
                    if cmd in ("EHLO", "HELO"):
                        self.reply("250-fake")
                        if starttls:
                            self.reply("250-STARTTLS")
                        self.reply("250-AUTH PLAIN")
                        self.reply("250 8BITMIME")
                    elif cmd == "AUTH":
                        blob = line.split(" ")[-1]
                        outer.auth.append(base64.b64decode(blob).decode())
                        self.reply("235 ok" if auth_ok else f"535 bad credentials for {SMTP_USER} / {SMTP_PASSWORD}")
                    elif cmd == "MAIL":
                        self.reply("250 ok")
                    elif cmd == "RCPT":
                        outer.rcpts.append(line.split(":", 1)[1].strip())
                        self.reply(f"550 no such user {TO}" if reject_rcpt else "250 ok")
                    elif cmd == "DATA":
                        self.reply("354 go")
                        chunks = []
                        while True:
                            part = self.rfile.readline()
                            if part in (b".\r\n", b""):
                                break
                            chunks.append(part[1:] if part.startswith(b"..") else part)
                        outer.messages.append(b"".join(chunks))
                        self.reply("250 queued")
                    elif cmd == "QUIT":
                        self.reply("221 bye")
                        return
                    else:
                        self.reply("250 ok")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def parsed(self, n: int = -1):
        return email.message_from_bytes(self.messages[n], policy=email.policy.default)


class NoTLSSMTP(smtplib.SMTP):
    """Real smtplib over a socket, but STARTTLS is a no-op (the fake server has no certificate)."""

    def starttls(self, *a, **k):  # noqa: D401
        self.starttls_seen = True
        return 220, b"ready"


@pytest.fixture
def smtp(monkeypatch):
    srv = FakeSMTP()
    calls: list[tuple] = []

    def opener(host, port, timeout, implicit_tls, local_hostname):
        calls.append((host, port, timeout, implicit_tls, local_hostname))
        return NoTLSSMTP("127.0.0.1", srv.port, timeout=5)

    monkeypatch.setattr(deliver, "_open_smtp", opener)
    srv.calls = calls
    yield srv
    srv.close()


class Hook:
    """httpx.MockTransport that records requests and answers from ``responses`` (a status or an exception)."""

    def __init__(self, *responses) -> None:
        self.responses = list(responses) or [204]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return httpx.Response(r)

    @property
    def n(self) -> int:
        return len(self.requests)

    def payload(self, i: int = -1):
        return json.loads(self.requests[i].content)


@pytest.fixture
def hook(monkeypatch):
    h = Hook()
    monkeypatch.setattr(deliver, "HTTP_TRANSPORT", httpx.MockTransport(h))
    return h


def use_hook(monkeypatch, *responses) -> Hook:
    h = Hook(*responses)
    monkeypatch.setattr(deliver, "HTTP_TRANSPORT", httpx.MockTransport(h))
    return h


# --- data ---------------------------------------------------------------------------------------------

def body_with(n_convs: int = 1, subject: str = "Encoding choice <b>for</b> the draft", status_line: str = "Authors converge on option B") -> dict:
    return {
        "period": {"start": "2026-10-01T07:00+09:00", "end": "2026-10-02T07:00+09:00", "tz": "Asia/Tokyo"},
        "language": "en", "generated_at": "2026-10-01T22:00:00+00:00", "quiet": False,
        "health": [{"kind": "llm_disabled", "detail": ""}],
        "conversations": [
            {"id": i, "list": "ops-wg", "subject": f"{subject} {i}", "state": {"version": 1, "status_line": status_line, "changed_since_last": "B gained support"},
             "new_messages": 2, "participants": ["Alice Example"], "score": 1.0,
             "web_url": f"https://digest.example.com/conversations/{i}", "latest_url": "javascript:alert(1)"}
            for i in range(1, n_convs + 1)
        ],
        "events": [{"kind": "ietf.last_call", "label": "Last Call", "ref": "draft-example-foo-03", "title": "Last Call: draft-example-foo",
                    "occurred_at": "2026-10-01T12:00+00:00", "url": "https://datatracker.example.org/doc/draft-example-foo/", "list": "ops-wg"}],
        "items": [{"id": 1, "title": "An article <i>x</i>", "url": "javascript:alert(2)", "feed": "https://blog.example.com/feed", "summary": "A short summary."}],
        "counts": {"conversations": n_convs, "items": 1, "events": 1, "events_total": 1},
    }


def mkdigest(session, *, days_ago: int = 0, body: dict | None = None, language: str = "en") -> Digest:
    end = END - timedelta(days=days_ago)
    d = Digest(period_start=end - timedelta(hours=24), period_end=end, language=language, body_json=body or body_with())
    session.add(d)
    session.commit()
    return d


def rows(session) -> dict[tuple[int, str], Delivery]:
    session.expire_all()
    return {(d.digest_id, d.channel): d for d in session.scalars(select(Delivery))}


# --- settings -----------------------------------------------------------------------------------------

def test_enabled_channels_parsing_and_secret_repr():
    s = base_settings(delivery_channels=" Email, webhook,,WEB ", webhook_url=WEBHOOK, smtp_password=SMTP_PASSWORD, smtp_user=SMTP_USER, digest_to=TO)
    assert s.enabled_channels == {"email", "webhook", "web"}
    assert base_settings().enabled_channels == {"web", "atom"}
    rep = repr(s) + str(s)
    for secret in (WEBHOOK, SMTP_PASSWORD, SMTP_USER, TO):
        assert secret not in rep


@pytest.mark.parametrize("kw,channel,expected", [
    ({}, "email", None), ({}, "webhook", None),
    ({"webhook_url": ""}, "webhook", "not_configured"),
    ({"webhook_url": "https://example.com/change-me-webhook"}, "webhook", "not_configured"),
    ({"webhook_url": "javascript:alert(1)"}, "webhook", "not_configured"),
    ({"smtp_host": ""}, "email", "not_configured"),
    ({"smtp_host": "change-me-host"}, "email", "not_configured"),
    ({"smtp_from": ""}, "email", "not_configured"),
    ({"digest_to": "change-me@example.com"}, "email", "not_configured"),
    ({"smtp_user": "change-me-smtp-user"}, "email", "not_configured"),
    ({"smtp_password": "change-me-smtp-password"}, "email", "not_configured"),
    ({"smtp_user": "", "smtp_password": ""}, "email", None),  # no login is allowed
])
def test_config_error(kw, channel, expected):
    assert deliver.config_error(cfg(**kw), channel) == expected


# --- email content ------------------------------------------------------------------------------------

def test_email_has_text_and_html_with_escaped_content_and_http_links_only():
    m = build_email(cfg(), body_with(2), "en", 7)
    assert m["Subject"] == "Sounding digest — 2026-10-01 07:00 - 2026-10-02 07:00 (Asia/Tokyo)"
    assert m["To"] == TO and m["From"] == FROM
    assert m["Message-ID"].endswith("@sounding.invalid>")
    plain = m.get_body(preferencelist=("plain",)).get_content()
    html_part = m.get_body(preferencelist=("html",)).get_content()
    assert m.get_content_type() == "multipart/alternative"
    assert "Encoding choice <b>for</b> the draft 1" in plain and "https://digest.example.com/conversations/2" in plain
    assert "https://digest.example.com/digests/7" in plain and "https://digest.example.com/digests/7" in html_part
    assert "javascript:" not in plain.lower() and "javascript:" not in html_part.lower()
    assert "&lt;b&gt;for&lt;/b&gt;" in html_part and "<b>for</b>" not in html_part  # escaped
    assert "&lt;i&gt;x&lt;/i&gt;" in html_part
    assert 'href="https://digest.example.com/conversations/1"' in html_part
    assert 'href="https://datatracker.example.org/doc/draft-example-foo/"' in html_part
    assert 'href="javascript' not in html_part


def test_email_subject_and_body_in_japanese():
    body = body_with()
    body["language"] = "ja"
    m = build_email(cfg(), body, "ja", 1)
    assert m["Subject"].startswith("Sounding ダイジェスト — ")
    assert "動きがあった会話" in m.get_body(preferencelist=("plain",)).get_content()


def test_email_multiple_recipients_and_header_injection_is_refused():
    m = build_email(cfg(digest_to="a@example.com, b@example.com"), body_with(), "en", 1)
    assert m["To"] == "a@example.com, b@example.com"
    with pytest.raises(ValueError):
        build_email(cfg(digest_to="a@example.com\r\nBcc: x@example.com"), body_with(), "en", 1)


def test_send_email_over_fake_smtp(smtp):
    deliver.send_email(cfg(), body_with(), "en", 3)
    assert len(smtp.messages) == 1 and smtp.calls == [("smtp.example.com", 587, 30.0, False, "digest.example.com")]
    assert smtp.auth == [f"\0{SMTP_USER}\0{SMTP_PASSWORD}"]
    assert smtp.rcpts == [f"<{TO}>"]
    got = smtp.parsed()
    assert "Encoding choice" in got.get_body(preferencelist=("plain",)).get_content()
    assert got.get_body(preferencelist=("html",)) is not None


def test_send_email_no_login_without_user(smtp):
    deliver.send_email(cfg(smtp_user="", smtp_password=""), body_with(), "en", 3)
    assert smtp.auth == [] and len(smtp.messages) == 1


def test_port_465_is_implicit_tls_and_certificates_are_verified(monkeypatch):
    seen = {}

    class Rec:
        def __init__(self, host, port, timeout=None, local_hostname=None, context=None):
            seen.update(host=host, port=port, ctx=context)
            raise OSError("no network in tests")

    monkeypatch.setattr(smtplib, "SMTP_SSL", Rec)
    with pytest.raises(DeliveryError) as ei:
        deliver.send_email(cfg(smtp_port=465), body_with(), "en", 1)
    assert ei.value.code == "connect_error" and seen["port"] == 465
    assert seen["ctx"].verify_mode == ssl.CERT_REQUIRED and seen["ctx"].check_hostname


def test_server_without_starttls_fails_and_never_falls_back_to_plaintext():
    srv = FakeSMTP(starttls=False)
    try:
        orig = deliver._open_smtp
        deliver._open_smtp = lambda host, port, timeout, implicit, local: smtplib.SMTP("127.0.0.1", srv.port, timeout=5)
        try:
            with pytest.raises(DeliveryError) as ei:
                deliver.send_email(cfg(), body_with(), "en", 1)
        finally:
            deliver._open_smtp = orig
        assert ei.value.code == "starttls_unavailable"
        assert srv.auth == [] and srv.messages == []  # neither the password nor the digest was sent
    finally:
        srv.close()


def test_starttls_context_verifies_certificates(monkeypatch):
    seen = {}

    class Fake:
        def ehlo(self): pass
        def has_extn(self, name): return True
        def starttls(self, context=None): seen["ctx"] = context
        def login(self, u, p): pass
        def send_message(self, m): pass
        def quit(self): pass

    monkeypatch.setattr(deliver, "_open_smtp", lambda *a: Fake())
    deliver.send_email(cfg(), body_with(), "en", 1)
    assert seen["ctx"].verify_mode == ssl.CERT_REQUIRED and seen["ctx"].check_hostname


@pytest.mark.parametrize("server_kw,code", [({"auth_ok": False}, "smtp_auth_failed"), ({"reject_rcpt": True}, "smtp_refused")])
def test_smtp_errors_become_tokens_without_server_text(monkeypatch, server_kw, code):
    srv = FakeSMTP(**server_kw)
    monkeypatch.setattr(deliver, "_open_smtp", lambda *a: NoTLSSMTP("127.0.0.1", srv.port, timeout=5))
    try:
        with pytest.raises(DeliveryError) as ei:
            deliver.send_email(cfg(), body_with(), "en", 1)
    finally:
        srv.close()
    exc = ei.value
    assert exc.code == code and str(exc) == code
    assert exc.__cause__ is None and exc.__suppress_context__  # the smtplib exception (it quotes the server text) is dropped


def test_connect_failure_and_timeout_tokens(monkeypatch):
    monkeypatch.setattr(deliver, "_open_smtp", lambda *a: (_ for _ in ()).throw(ConnectionRefusedError("refused smtp.example.com")))
    with pytest.raises(DeliveryError) as ei:
        deliver.send_email(cfg(), body_with(), "en", 1)
    assert ei.value.code == "connect_error"
    monkeypatch.setattr(deliver, "_open_smtp", lambda *a: (_ for _ in ()).throw(TimeoutError("timed out")))
    with pytest.raises(DeliveryError) as ei:
        deliver.send_email(cfg(), body_with(), "en", 1)
    assert ei.value.code == "timeout"


# --- webhook ------------------------------------------------------------------------------------------

def test_webhook_flavor_by_host():
    assert webhook_flavor(DISCORD) == "discord"
    assert webhook_flavor("https://discordapp.com/api/webhooks/1/x") == "discord"
    assert webhook_flavor("https://ptb.discord.com/api/webhooks/1/x") == "discord"
    assert webhook_flavor("https://evildiscord.com/x") == "generic"
    assert webhook_flavor(SLACK) == "slack"
    assert webhook_flavor(WEBHOOK) == "generic"


def test_webhook_payload_keys_per_platform():
    assert webhook_payload(DISCORD, "hi <x>") == {"content": "hi <x>", "allowed_mentions": {"parse": []}}
    assert webhook_payload(SLACK, "a & <b>") == {"text": "a &amp; &lt;b&gt;"}
    assert webhook_payload(WEBHOOK, "hi") == {"content": "hi", "text": "hi"}


def test_webhook_message_content_and_link_last():
    s = cfg()
    message = build_webhook_message(s, body_with(2), "en", 7)
    lines = message.split("\n")
    assert lines[0].startswith("Sounding digest  2026-10-01 07:00 - 2026-10-02 07:00")
    assert "! LLM is disabled" in message
    assert "* Encoding choice <b>for</b> the draft 1 - Authors converge on option B" in message
    assert "https://digest.example.com/conversations/2" in message
    assert "Procedural updates: 1, related articles: 1" in message
    assert lines[-1] == "https://digest.example.com/digests/7"
    assert "javascript" not in message


def test_webhook_message_is_capped_and_keeps_the_link():
    body = body_with(80, subject="A very long subject " * 10, status_line="A very long status line " * 20)
    body["health"] = [{"kind": "job_failed", "detail": "fetch: " + "x" * 600}]
    message = build_webhook_message(cfg(), body, "en", 12)
    assert len(message) <= 2000 and len(message) <= deliver.WEBHOOK_LIMIT
    assert message.endswith("https://digest.example.com/digests/12")
    assert "* A very long subject" in message and "... +" in message  # truncated cleanly, with a count of what is left
    assert message.count("\n* ") + message.startswith("* ") < 80
    # even a huge health section keeps the link last
    body["health"] = [{"kind": "job_failed", "detail": "y" * 5000}] * 30
    m2 = build_webhook_message(cfg(), body, "en", 12)
    assert len(m2) <= deliver.WEBHOOK_LIMIT and m2.endswith("/digests/12")


def test_webhook_message_japanese_and_quiet():
    body = body_with(0)
    body.update(quiet=True, events=[], items=[], language="ja")
    body["counts"] = {}
    m = build_webhook_message(cfg(), body, "ja", 2)
    assert "Sounding ダイジェスト" in m and "動きがありませんでした" in m and m.endswith("/digests/2")


@pytest.mark.parametrize("url,keys", [
    (DISCORD, {"content", "allowed_mentions"}), (SLACK, {"text"}), (WEBHOOK, {"content", "text"}),
])
def test_send_webhook_posts_json_once(monkeypatch, url, keys):
    h = use_hook(monkeypatch, 204)
    deliver.send_webhook(cfg(webhook_url=url), body_with(), "en", 5)
    assert h.n == 1 and str(h.requests[0].url) == url and h.requests[0].method == "POST"
    assert h.requests[0].headers["content-type"].startswith("application/json")
    assert set(h.payload()) == keys


@pytest.mark.parametrize("response,code", [
    (404, "http_404"), (500, "http_500"), (302, "http_302"), (429, "http_429"),
    (httpx.ReadTimeout("slow " + WEBHOOK), "timeout"), (httpx.ConnectTimeout("t " + WEBHOOK), "timeout"),
    (httpx.ConnectError("no route to " + WEBHOOK), "connect_error"),
    (httpx.UnsupportedProtocol("bad " + WEBHOOK), "invalid_url"), (httpx.DecodingError("x " + WEBHOOK), "http_error"),
])
def test_send_webhook_failures_are_tokens_and_do_not_follow_redirects(monkeypatch, response, code):
    h = use_hook(monkeypatch, response)
    with pytest.raises(DeliveryError) as ei:
        deliver.send_webhook(cfg(), body_with(), "en", 5)
    assert ei.value.code == code and "ZZHOOKTOKEN" not in str(ei.value) and ei.value.__cause__ is None
    assert h.n == 1  # a redirect is a failure, not followed


# --- run_digest: sent once, retry, limit ---------------------------------------------------------------

def webhook_cfg(**kw):
    return cfg(delivery_channels="web,webhook,atom", **kw)


def run(sf, settings, now=NOW, **kw):
    return jobs.run_digest(sf, settings, profile=prof(), now=now, **kw)


def test_quiet_day_digest_is_delivered_and_a_rerun_sends_nothing_twice(session, sf, hook, smtp):
    s = cfg()
    rep = run(sf, s)
    assert rep.body["quiet"] is True
    assert rep.delivery == "email=sent webhook=sent"
    assert hook.n == 1 and len(smtp.messages) == 1
    again = run(sf, s)
    assert again.rebuilt and again.delivery == "email=already_sent webhook=already_sent"
    assert hook.n == 1 and len(smtp.messages) == 1
    r = rows(session)
    assert {k[1]: (v.status, v.attempts, v.error) for k, v in r.items()} == {"email": ("sent", 1, None), "webhook": ("sent", 1, None)}
    assert all(v.last_attempt_at is not None for v in r.values())
    run_row = session.scalars(select(JobRun).where(JobRun.job == "digest").order_by(JobRun.id.desc())).first()
    assert run_row.status == "ok" and run_row.counts["delivery"] == again.delivery


def test_real_digest_email_has_summary_and_link_but_no_message_body(session, sf, smtp):
    wg = mksrc(session, "ietf_mail", "sidrops")
    it = msg(session, wg, "[sidrops] ASPA verification", body=BODY_MARKER)
    add_state(session, it.conversation_id, status_line="WGLC in progress")
    rep = run(sf, cfg(delivery_channels="email"))
    assert rep.delivery == "email=sent" and rep.body["conversations"]
    got = smtp.parsed()
    plain = got.get_body(preferencelist=("plain",)).get_content()
    html_part = got.get_body(preferencelist=("html",)).get_content()
    assert "[sidrops] ASPA verification" in plain and "WGLC in progress" in plain
    assert f"https://digest.example.com/conversations/{it.conversation_id}" in plain
    assert "ZZBODYMARKER" not in plain and "ZZBODYMARKER" not in html_part
    assert TO not in smtp.messages[0].decode().split("\r\n\r\n", 1)[1]  # no address in the body either


def test_failure_then_retry_on_the_next_run_then_success(session, sf, monkeypatch):
    h = use_hook(monkeypatch, 404, 204)
    s = webhook_cfg()
    first = run(sf, s)
    assert first.delivery == "webhook=failed(http_404)"
    r = rows(session)[(first.digest_id, "webhook")]
    assert (r.status, r.attempts, r.error) == ("failed", 1, "http_404")
    assert session.scalars(select(JobRun).where(JobRun.job == "digest")).one().status == "ok"  # the job itself did not fail
    second = run(sf, s)  # same period: rebuilt in place, retried
    assert second.digest_id == first.digest_id and second.delivery == "webhook=sent"
    r = rows(session)[(first.digest_id, "webhook")]
    assert (r.status, r.attempts, r.error) == ("sent", 2, None) and h.n == 2
    run(sf, s)
    assert h.n == 2  # not a third time


def test_earlier_digest_is_retried_by_the_next_digest_run(session, sf, monkeypatch):
    h = use_hook(monkeypatch, 500, 204)
    s = webhook_cfg()
    a = run(sf, s)
    assert a.delivery == "webhook=failed(http_500)"
    b = run(sf, s, now=NOW + timedelta(days=1))
    assert b.digest_id != a.digest_id
    assert b.delivery == f"webhook=sent; retried digest {a.digest_id}: webhook=sent"
    r = rows(session)
    assert r[(a.digest_id, "webhook")].status == "sent" and r[(a.digest_id, "webhook")].attempts == 2
    assert r[(b.digest_id, "webhook")].attempts == 1 and h.n == 3


def test_retry_limit_is_two_attempts_and_a_window(session, sf, monkeypatch):
    h = use_hook(monkeypatch, 500)
    s = webhook_cfg()
    a = run(sf, s)
    run(sf, s, now=NOW + timedelta(days=1))  # A retried: attempts 2, fails again
    assert rows(session)[(a.digest_id, "webhook")].attempts == 2 and h.n == 3
    run(sf, s, now=NOW + timedelta(days=2))  # A is not tried again; B (attempts 1) is, C is new
    r = rows(session)
    assert r[(a.digest_id, "webhook")].attempts == 2 and r[(a.digest_id, "webhook")].status == "failed"
    assert [d.attempts for k, d in sorted(r.items())] == [2, 2, 1]
    assert h.n == 3 + 2
    # an automatic call on a given-up delivery reports it and sends nothing
    before = h.n
    assert deliver_digest(sf, s, a.digest_id) == "webhook=gave_up(http_500)"
    assert h.n == before
    # a digest that ended more than the window ago is not retried by a later run
    old = mkdigest(session, days_ago=30)
    session.add(Delivery(digest_id=old.id, channel="webhook", status="failed", attempts=1, error="timeout"))
    session.commit()
    run(sf, s, now=NOW + timedelta(days=3))
    assert rows(session)[(old.id, "webhook")].attempts == 1


def test_channels_are_independent(session, sf, smtp, monkeypatch):
    use_hook(monkeypatch, 500)
    rep = run(sf, cfg())
    assert rep.delivery == "email=sent webhook=failed(http_500)"
    assert len(smtp.messages) == 1
    again = run(sf, cfg())
    assert again.delivery == "email=already_sent webhook=failed(http_500)"  # only the failed one is retried
    assert len(smtp.messages) == 1


def test_not_configured_is_recorded_per_channel_and_does_not_fail_the_job(session, sf, hook):
    s = cfg(webhook_url="https://example.com/change-me-webhook", smtp_host="", digest_to="change-me")
    rep = run(sf, s)
    assert rep.status == "ok" and rep.delivery == "email=failed(not_configured) webhook=failed(not_configured)"
    r = rows(session)
    assert {(v.status, v.error, v.attempts) for v in r.values()} == {("failed", "not_configured", 1)}
    assert hook.n == 0
    # only the broken channel is affected
    ok = run(sf, cfg(smtp_host=""), now=NOW + timedelta(days=1))
    assert "webhook=sent" in ok.delivery and "email=failed(not_configured)" in ok.delivery


def test_unexpected_exception_in_a_sender_is_contained(session, sf, monkeypatch):
    monkeypatch.setitem(deliver.SENDERS, "webhook", lambda *a: (_ for _ in ()).throw(RuntimeError("boom " + WEBHOOK)))
    rep = run(sf, webhook_cfg())
    assert rep.status == "ok" and rep.delivery == "webhook=failed(error)"


def test_no_sending_channel_enabled(session, sf):
    rep = run(sf, base_settings())
    assert rep.delivery == jobs.NO_CHANNELS and rows(session) == {}


def test_deliver_false_sends_nothing(session, sf, hook, smtp):
    rep = run(sf, cfg(), deliver=False)
    assert rep.delivery is None and hook.n == 0 and smtp.messages == [] and rows(session) == {}


def test_rebuild_does_not_reset_deliveries(session, sf, hook):
    s = webhook_cfg()
    a = run(sf, s)
    run(sf, s, now=NOW + timedelta(hours=1))
    assert rows(session)[(a.digest_id, "webhook")].attempts == 1 and hook.n == 1


# --- CLI ---------------------------------------------------------------------------------------------

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    profile = tmp_path / "profile.yaml"
    profile.write_text("ietf:\n  working_groups: [sidrops]\nprofile:\n  language: en\n")
    for k, v in {
        "PROFILE_PATH": str(profile), "LLM_PROVIDER": "none", "PUBLIC_BASE_URL": "https://digest.example.com",
        "DELIVERY_CHANNELS": "web,webhook,atom", "WEBHOOK_URL": WEBHOOK,
    }.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_cli_digest_prints_delivery_and_no_deliver_sends_nothing(session, sf, cli_env, monkeypatch, capsys):
    h = use_hook(monkeypatch, 204)
    assert main(["digest", "--once", "--no-deliver"]) == 0
    out = capsys.readouterr().out
    assert "delivery:" not in out and h.n == 0 and rows(session) == {}
    assert main(["digest", "--once"]) == 0
    out = capsys.readouterr().out
    assert "delivery: webhook=sent" in out and h.n == 1
    assert WEBHOOK not in out and "ZZHOOKTOKEN" not in out


def test_cli_deliver_manual_resend(session, sf, cli_env, monkeypatch, capsys):
    h = use_hook(monkeypatch, 500, 500, 500, 204, 204)
    assert main(["digest", "--once"]) == 0
    capsys.readouterr()
    assert main(["digest", "--once"]) == 0  # second attempt (the retry) also fails
    capsys.readouterr()
    r = rows(session)[(1, "webhook")]
    assert (r.status, r.attempts) == ("failed", 2) and h.n == 2
    # the automatic limit is reached, but a person can still resend
    assert main(["deliver", "--digest", "1"]) == 1  # 500 again -> exit 1, error shown as a token
    out = capsys.readouterr().out
    assert "delivery: webhook=failed(http_500)" in out and h.n == 3
    assert main(["deliver", "--digest", "1", "--channel", "webhook"]) == 0
    assert "delivery: webhook=sent" in capsys.readouterr().out and rows(session)[(1, "webhook")].attempts == 4
    # sent channels are skipped unless --force
    assert main(["deliver", "--digest", "1"]) == 0
    assert "webhook=already_sent" in capsys.readouterr().out and h.n == 4
    assert main(["deliver", "--digest", "1", "--force"]) == 0
    assert "webhook=sent" in capsys.readouterr().out and h.n == 5
    # errors
    assert main(["deliver", "--digest", "99"]) == 1
    assert "no such digest" in capsys.readouterr().err
    assert main(["deliver", "--digest", "1", "--channel", "email"]) == 1  # not in DELIVERY_CHANNELS
    assert "not enabled" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["deliver", "--digest", "1", "--channel", "atom"])
    with pytest.raises(SystemExit):
        main(["deliver"])


def test_cli_status_shows_delivery_state_without_secrets(session, sf):
    s = cfg(delivery_channels="web,email,webhook,atom")
    d = mkdigest(session)
    session.add_all([
        Delivery(digest_id=d.id, channel="webhook", status="failed", attempts=1, last_attempt_at=NOW, error="http_404"),
        Delivery(digest_id=d.id, channel="email", status="sent", attempts=1, last_attempt_at=NOW),
    ])
    session.commit()
    out = format_status(session, s)
    assert "delivery webhook: failed digest=" in out and "error=http_404" in out
    assert "delivery email: sent digest=" in out
    assert "delivery web: in the database" in out and "delivery atom: in the database" in out
    for secret in SECRETS:
        assert secret not in out
    session.execute(text("TRUNCATE deliveries"))
    session.commit()
    assert "delivery email: (never)" in format_status(session, s)


# --- secrets -----------------------------------------------------------------------------------------

def all_text_in_db(session) -> str:
    chunks = []
    tables = session.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")).scalars().all()
    for t in tables:
        chunks += session.execute(text(f'SELECT x::text FROM "{t}" x')).scalars().all()  # noqa: S608 - names come from the catalog
    return "\n".join(chunks)


def test_secrets_never_appear_in_logs_db_errors_or_cli_output(session, sf, monkeypatch, caplog, capsys, tmp_path):
    caplog.set_level(logging.DEBUG)
    # webhook: the transport error carries the URL (as httpx's own messages can); a success is logged by httpx at INFO
    h = use_hook(monkeypatch, httpx.ConnectError("connect to " + WEBHOOK + " failed"), 204)
    # email: the server answers with the user and the password in its error text
    srv = FakeSMTP(auth_ok=False)
    monkeypatch.setattr(deliver, "_open_smtp", lambda *a: NoTLSSMTP("127.0.0.1", srv.port, timeout=5))
    try:
        s = cfg()
        first = run(sf, s)
        assert first.delivery == "email=failed(smtp_auth_failed) webhook=failed(connect_error)"
        second = run(sf, s)  # the retry: the webhook now succeeds, which makes httpx log its request line
        assert "webhook=sent" in second.delivery
    finally:
        srv.close()
    assert h.n == 2
    assert main(["status"]) == 0
    out = capsys.readouterr()
    logs = caplog.text + "\n" + out.out + out.err
    db = all_text_in_db(session)
    for secret in SECRETS + [WEBHOOK.split("/")[-1]]:
        assert secret not in logs, secret
        assert secret not in db, secret
    assert "HTTP Request" not in caplog.text  # httpx's per-request INFO line (it prints the URL) is silenced
    last = session.scalars(select(JobRun).where(JobRun.job == "digest").order_by(JobRun.id.desc())).first()
    assert "smtp_auth_failed" in json.dumps(last.counts)


def test_secret_check_helper_would_catch_a_leak(session):
    session.add(JobRun(job="x", started_at=NOW, finished_at=NOW, status="failed", error=SMTP_PASSWORD))
    session.commit()
    assert SMTP_PASSWORD in all_text_in_db(session)


def test_digest_not_found_and_no_digest_means_no_rows(session, sf, hook):
    assert deliver_digest(sf, webhook_cfg(), 999) == "digest_not_found"
    assert session.scalar(select(func.count()).select_from(Delivery)) == 0 and hook.n == 0


def test_unknown_channel_names_are_ignored_with_a_warning(session, sf, hook, caplog):
    d = mkdigest(session)
    assert deliver_digest(sf, cfg(delivery_channels="webhook,pigeon"), d.id) == "webhook=sent"
    assert "pigeon" in caplog.text

