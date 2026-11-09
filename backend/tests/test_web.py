"""Web UI, JSON API, login, Atom feed and compose wiring (spec phase 3, R6-R9, R11): real Postgres, FastAPI TestClient."""

from __future__ import annotations

import io
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import feedparser
import pytest
import yaml
from fastapi.testclient import TestClient

from sounding.cli import main as cli_main
from sounding.config import Settings
from sounding.db import get_session_factory
from sounding.models import (
    Conversation, ConversationState, Digest, Item, JobRun, LlmCall, Selection, Source,
)
from sounding.web import auth, queries
from sounding.web.app import create_app, safe_next

ROOT = Path(__file__).resolve().parents[2]
SECRET = "unit-test-session-secret-" + "k" * 24
PASSWORD = "correct horse battery staple"
PW_HASH = auth.hash_password(PASSWORD, n=2**4)  # cheap parameters: the default cost is covered in test_hash_cli
BODY_SENTENCE = "ZZBODYMARKER the full body text of the message that must never be served"
ADDR = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
BASE = "https://digest.example.com"
END = datetime(2026, 10, 1, 22, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 2, 3, 0, tzinfo=UTC)


def make_settings(tmp_path: Path, **kw) -> Settings:
    prof = tmp_path / "profile.yaml"
    if "profile_path" not in kw and not prof.exists():
        prof.write_text("profile:\n  language: en\n")
    base = dict(
        database_url="postgresql+psycopg://u:p@localhost:1/none", session_secret=SECRET, web_password_hash=PW_HASH,
        public_base_url=BASE, profile_path=str(prof), llm_provider="none",
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


def state_json(version: int) -> dict:
    return {
        "status_line": f"Authors converge on option B (v{version})",
        "issues": [{
            "issue": "Which encoding to use", "status": "leaning" if version == 2 else "open", "evidence": "Both implementers confirmed interop",
            "refs": ["m1"],
            "positions": [{"stance": "Prefer TLV", "by": ["Alice Example", "bob@example.com"], "refs": ["m1", "m2"]}],
        }],
        "agreed": [{"text": "Scope excludes multicast", "refs": ["m2"]}],
        "open_questions": ["Is a registry needed?"],
        "next": ["Chairs run a consensus call"],
        "changed_since_last": "Option B gained support" if version == 2 else None,
        "sources_used": ["m1", "m2"],
        "sources": [
            {"ref": "m1", "url": "https://mailarchive.example.org/arch/msg/ops-wg/1", "date": "2026-10-01T10:00", "author_name": "Alice Example"},
            {"ref": "m2", "url": "javascript:alert(document.cookie)", "date": "2026-10-01T11:00", "author_name": "bob@example.com"},
        ],
        "process_state": None,
    }


@pytest.fixture
def seeded(session):
    """A digest, a conversation with two state versions, selections, sources and job runs. Returns ids."""
    ml = Source(kind="ietf_mail", key="ops-wg", last_fetched_at=NOW, last_status="ok")
    rss = Source(kind="rss", key="https://blog.example.com/feed", url="https://blog.example.com/feed", last_fetched_at=NOW, last_status="ok")
    session.add_all([ml, rss])
    session.flush()
    conv = Conversation(
        source_id=ml.id, subject="Re: Encoding choice for the new draft", base_subject="Encoding choice", first_message_at=END - timedelta(hours=5),
        last_message_at=END - timedelta(hours=1), message_count=2,
    )
    session.add(conv)
    session.flush()
    for n, who in enumerate(("Alice Example", "Bob Example"), start=1):
        session.add(Item(
            source_id=ml.id, external_id=f"m{n}", url=f"https://mailarchive.example.org/arch/msg/ops-wg/{n}", title="Encoding choice",
            author_name=who, published_at=END - timedelta(hours=6 - n), body_text=BODY_SENTENCE, conversation_id=conv.id,
        ))
    art = Item(
        source_id=rss.id, external_id="a1", url="javascript:alert(1)", title="An article", published_at=END - timedelta(hours=2),
        body_text=BODY_SENTENCE,
    )
    session.add(art)
    session.flush()
    for v in (1, 2):
        session.add(ConversationState(
            conversation_id=conv.id, language="en", version=v, state_json=state_json(v),
            changed_since_last="Option B gained support" if v == 2 else None, covered_item_ids=[], last_item_published_at=END,
            prompt_version="t", model=f"model-{v}", created_at=END + timedelta(minutes=v),
        ))
    body = {
        "period": {"start": "2026-10-01T07:00+09:00", "end": "2026-10-02T07:00+09:00", "tz": "Asia/Tokyo"},
        "language": "en", "generated_at": "2026-10-01T22:00:00+00:00", "quiet": False,
        "health": [{"kind": "llm_disabled", "detail": ""}, {"kind": "job_failed", "detail": "fetch: timeout"}],
        "conversations": [{
            "id": conv.id, "list": "ops-wg", "subject": "Encoding choice for the new draft",
            "state": {"version": 2, "status_line": "Authors converge on option B (v2)", "changed_since_last": "Option B gained support"},
            "new_messages": 2, "participants": ["Alice Example", "Bob Example"], "score": 7.0,
            "web_url": f"{BASE}/conversations/{conv.id}", "latest_url": "javascript:alert('x')",
        }],
        "events": [{
            "kind": "ietf.last_call", "label": "Last Call", "ref": "draft-example-foo-03", "title": "Last Call: draft-example-foo",
            "occurred_at": "2026-10-01T12:00+00:00", "url": "https://datatracker.example.org/doc/draft-example-foo/", "list": "ops-wg",
        }],
        "items": [{"id": art.id, "title": "An article <b>bold</b>", "url": "javascript:alert(1)", "feed": "https://blog.example.com/feed", "summary": "A short summary."}],
        "counts": {"conversations": 1, "items": 1, "events": 1},
    }
    digest = Digest(period_start=END - timedelta(hours=24), period_end=END, language="en", body_json=body, created_at=NOW)
    session.add(digest)
    session.flush()
    session.add(Selection(
        digest_id=digest.id, target_kind="conversation", target_id=conv.id, included=True, score=7.0, decided_by="rule",
        reasons_json={"rule": "working_group", "components": {"messages": 2, "participants": 2}},
    ))
    session.add_all([
        JobRun(job="fetch", started_at=NOW - timedelta(hours=1), finished_at=NOW - timedelta(hours=1), status="ok"),
        JobRun(job="fetch", started_at=NOW - timedelta(hours=3), finished_at=NOW, status="failed", error="timeout"),
        JobRun(job="digest", started_at=NOW, finished_at=NOW, status="ok"),
    ])
    session.add(LlmCall(
        purpose="x", provider="fake", model="m", tier="main", request_hash="h", status="ok", cost_usd=0.25, created_at=datetime.now(UTC),
    ))
    session.commit()
    return {"digest": digest.id, "conv": conv.id}


def client(tmp_path, *, login: bool = False, **kw) -> TestClient:
    app = create_app(make_settings(tmp_path, **kw), get_session_factory())
    c = TestClient(app, base_url=BASE, follow_redirects=False)
    if login:
        r = c.post("/login", content=f"password={PASSWORD}&next=%2F", headers={"content-type": "application/x-www-form-urlencoded"})
        assert r.status_code == 303
    return c


def form(c: TestClient, path: str, **fields: str):
    from urllib.parse import urlencode

    return c.post(path, content=urlencode(fields), headers={"content-type": "application/x-www-form-urlencoded"})


def all_pages(ids):
    return ["/", "/digests", f"/digests/{ids['digest']}", f"/conversations/{ids['conv']}", "/status", "/api/digests", "/api/digests/latest",
            f"/api/digests/{ids['digest']}", f"/api/conversations/{ids['conv']}", "/api/status"]


# --- pages and API ---------------------------------------------------------------------------------

def test_pages_and_api_render(seeded, tmp_path):
    c = client(tmp_path, login=True)
    r = c.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    for needle in ("Encoding choice for the new draft", "Authors converge on option B (v2)", "Option B gained support", "Alice Example",
                   "Last Call: draft-example-foo", "A short summary.", "LLM is disabled", "Job failed: fetch: timeout", "2 new"):
        assert needle in r.text, needle
    # HTML in a title is escaped, never interpreted
    assert "An article &lt;b&gt;bold&lt;/b&gt;" in r.text and "<b>bold</b>" not in r.text
    assert c.get(f"/digests/{seeded['digest']}").status_code == 200
    d = c.get("/digests")
    assert d.status_code == 200 and f"/digests/{seeded['digest']}" in d.text

    conv = c.get(f"/conversations/{seeded['conv']}")
    assert conv.status_code == 200
    for needle in ("Encoding choice for the new draft", "Authors converge on option B (v2)", "Which encoding to use", "Prefer TLV", "Scope excludes multicast",
                   "Is a registry needed?", "Chairs run a consensus call", "model-1", "model-2", "working_group", "mailarchive.example.org/arch/msg/ops-wg/1",
                   "Version history", "Why it was (not) selected"):
        assert needle in conv.text, needle

    st = c.get("/status")
    assert st.status_code == 200
    for needle in ("fetch", "ops-wg", "https://blog.example.com/feed", "$0.2500", "$1.00", "/feed/", ".atom", "timeout"):
        assert needle in st.text, needle


def test_api_json_shapes(seeded, tmp_path):
    c = client(tmp_path, login=True)
    lst = c.get("/api/digests").json()
    assert lst["digests"][0]["id"] == seeded["digest"] and "body" not in lst["digests"][0] and lst["has_more"] is False
    latest = c.get("/api/digests/latest").json()
    assert latest["id"] == seeded["digest"] and latest["body"]["conversations"][0]["id"] == seeded["conv"]
    assert c.get(f"/api/digests/{seeded['digest']}").json()["id"] == seeded["digest"]
    conv = c.get(f"/api/conversations/{seeded['conv']}").json()
    assert conv["list"] == "ops-wg" and conv["state"]["version"] == 2
    assert [v["version"] for v in conv["versions"]] == [2, 1]
    assert conv["versions"][0]["model"] == "model-2" and conv["versions"][0]["changed_since_last"] == "Option B gained support"
    assert conv["selections"][0]["included"] is True and conv["selections"][0]["reasons"]["rule"] == "working_group"
    assert conv["state"]["issues"][0]["positions"][0]["refs"] == ["m1", "m2"]
    status = c.get("/api/status").json()
    jobs = {j["job"]: j for j in status["jobs"]}
    assert jobs["fetch"]["last_ok_at"] and jobs["fetch"]["last_failed_at"] and jobs["fetch"]["last_error"] == "timeout"
    assert jobs["summarize"]["last_ok_at"] is None
    src = {s["key"]: s for s in status["sources"]}
    assert src["ops-wg"]["items"] == 2 and src["ops-wg"]["conversations"] == 1
    assert status["llm"]["spend_today_usd"] == "0.2500" and status["llm"]["budget_usd"] == "1.00" and status["llm"]["calls_today"]["ok"] == 1
    assert status["last_digest"]["id"] == seeded["digest"] and status["atom_url"].startswith(BASE + "/feed/")


def test_pages_use_profile_language(seeded, tmp_path):
    (tmp_path / "ja.yaml").write_text("profile:\n  language: ja\n")
    c = client(tmp_path, login=True, profile_path=str(tmp_path / "ja.yaml"))
    r = c.get("/")
    assert '<html lang="ja">' in r.text and "動きがあった会話" in r.text
    assert "ダイジェスト一覧" in c.get("/digests").text


def test_bad_profile_falls_back_to_en_and_logs_once(seeded, tmp_path, caplog):
    (tmp_path / "bad.yaml").write_text("profile: [unclosed\n")
    c = client(tmp_path, login=True, profile_path=str(tmp_path / "bad.yaml"))
    with caplog.at_level("ERROR", logger="sounding.web"):
        assert "Active conversations" in c.get("/").text
        c.get("/status")
    assert len([r for r in caplog.records if "profile unreadable" in r.message]) == 1


def test_unknown_ids_are_404(seeded, tmp_path):
    c = client(tmp_path, login=True)
    for path in ("/digests/9999", "/conversations/9999", "/digests/abc", "/nope"):
        assert c.get(path).status_code == 404, path
    for path in ("/api/digests/9999", "/api/conversations/9999"):
        r = c.get(path)
        assert r.status_code == 404 and r.json()["detail"]
    assert c.get("/api/digests/abc").status_code == 422


def test_empty_states(session, tmp_path):
    c = client(tmp_path, login=True)
    r = c.get("/")
    assert r.status_code == 200 and "No digest yet" in r.text
    assert "No digests yet" in c.get("/digests").text
    assert c.get("/api/digests/latest").status_code == 404
    assert c.get("/api/digests").json() == {"page": 1, "size": 30, "has_more": False, "digests": []}
    assert c.get("/status").status_code == 200


def test_quiet_digest_shows_quiet_text(session, tmp_path):
    body = {"period": {"start": "2026-10-01T07:00+09:00", "end": "2026-10-02T07:00+09:00", "tz": "Asia/Tokyo"}, "language": "en",
            "generated_at": "x", "quiet": True, "health": [], "conversations": [], "events": [], "items": [], "counts": {}}
    session.add(Digest(period_start=END - timedelta(hours=24), period_end=END, language="en", body_json=body))
    session.commit()
    assert "No activity in this period" in client(tmp_path, login=True).get("/").text


def test_unfinished_digest_is_hidden(session, tmp_path):
    session.add(Digest(period_start=END - timedelta(hours=24), period_end=END, language="en", body_json={}))
    session.commit()
    c = client(tmp_path, login=True)
    assert "No digest yet" in c.get("/").text and c.get("/digests/1").status_code == 404


def test_digest_list_paging(session, tmp_path):
    body = {"period": {"start": "a", "end": "b", "tz": "UTC"}, "quiet": True, "counts": {}}
    for i in range(35):
        session.add(Digest(period_start=END + timedelta(days=i), period_end=END + timedelta(days=i + 1), language="en", body_json=body))
    session.commit()
    c = client(tmp_path, login=True)
    p1, p2 = c.get("/api/digests").json(), c.get("/api/digests?page=2").json()
    assert len(p1["digests"]) == 30 and p1["has_more"] and len(p2["digests"]) == 5 and not p2["has_more"]
    assert "page=2" in c.get("/digests").text


# --- bodies, links, addresses, headers ----------------------------------------------------------

def test_bodies_never_appear_and_no_addresses(seeded, tmp_path):
    c = client(tmp_path, login=True)
    token = auth.feed_token(SECRET)
    for path in [*all_pages(seeded), f"/feed/{token}.atom"]:
        r = c.get(path)
        assert r.status_code == 200, path
        assert "ZZBODYMARKER" not in r.text, path
        assert not ADDR.search(r.text), (path, ADDR.search(r.text).group(0))
    assert "[address]" in c.get(f"/conversations/{seeded['conv']}").text


def test_javascript_urls_are_never_links(seeded, tmp_path):
    c = client(tmp_path, login=True)
    token = auth.feed_token(SECRET)
    for path in [*all_pages(seeded), f"/feed/{token}.atom"]:
        text = c.get(path).text
        assert "javascript:" not in text.lower(), path
        assert not re.search(r'href="\s*javascript', text, re.I), path
    # the http(s) ones are links
    assert 'href="https://mailarchive.example.org/arch/msg/ops-wg/1"' in c.get(f"/conversations/{seeded['conv']}").text
    assert 'href="https://datatracker.example.org/doc/draft-example-foo/"' in c.get("/").text


@pytest.mark.parametrize("url,ok", [
    ("https://example.org/a?b=1", True), ("http://example.org", True), ("HTTPS://EXAMPLE.ORG/x", True),
    ("javascript:alert(1)", False), ("JaVaScRiPt:alert(1)", False), (" javascript:alert(1)", False), ("java\tscript:alert(1)", False),
    ("data:text/html,x", False), ("ftp://example.org/f", False), ("//example.org/x", False), ("/relative", False), ("https:///nohost", False),
    ("https://exa mple.org", False), ("", False), (None, False), ("https://example.org/\nx", False),
])
def test_clean_url(url, ok):
    assert (queries.clean_url(url) is not None) is ok


def test_template_filter_blocks_unsafe_url_even_if_the_query_layer_misses_one(seeded, tmp_path, monkeypatch):
    monkeypatch.setattr("sounding.web.queries.clean_body", lambda b: b)  # raw digest body, javascript: URLs included
    c = client(tmp_path, login=True)
    text = c.get("/").text
    assert "javascript:" not in text.lower() and "Encoding choice for the new draft" in text


def test_security_headers_on_every_response(seeded, tmp_path):
    c = client(tmp_path, login=True)
    token = auth.feed_token(SECRET)
    anon = client(tmp_path)
    responses = [c.get(p) for p in [*all_pages(seeded), "/healthz", "/static/style.css", "/nope", "/digests/9999", f"/feed/{token}.atom", "/feed/bad.atom"]]
    responses += [anon.get("/"), anon.get("/api/status"), anon.get("/login")]
    for r in responses:
        assert r.headers["content-security-policy"] == "default-src 'self'; style-src 'self'; img-src 'self'; frame-ancestors 'none'; form-action 'self'"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "same-origin"
        assert r.headers["x-frame-options"] == "DENY"


def test_no_inline_styles_or_scripts(seeded, tmp_path):
    c = client(tmp_path, login=True)
    for p in ("/", "/status", f"/conversations/{seeded['conv']}", "/digests"):
        t = c.get(p).text
        assert "<script" not in t and " style=" not in t and "<style" not in t


def test_static_css_and_healthz(tmp_path):
    c = client(tmp_path)  # no login: both are open anyway
    css = c.get("/static/style.css")
    assert css.status_code == 200 and "prefers-color-scheme: dark" in css.text and "background: var(--bg)" in css.text
    h = c.get("/healthz")
    assert h.status_code == 200 and h.text == "ok"


def test_healthz_and_static_need_no_session_even_with_password(tmp_path):
    c = client(tmp_path)
    assert c.get("/healthz").status_code == 200 and c.get("/static/style.css").status_code == 200


# --- login ---------------------------------------------------------------------------------------

def test_pages_redirect_to_login_and_api_returns_401(seeded, tmp_path):
    c = client(tmp_path)
    r = c.get("/digests/1")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/digests/1"
    r = c.get("/digests?page=2")
    assert r.headers["location"] == "/login?next=/digests%3Fpage%3D2"
    for path in ("/api/digests", "/api/digests/latest", "/api/status", f"/api/conversations/{seeded['conv']}"):
        r = c.get(path)
        assert r.status_code == 401 and r.json() == {"detail": "login required"}
    assert c.get("/status").status_code == 303
    assert c.post("/logout").status_code == 303


def test_login_flow(seeded, tmp_path):
    c = client(tmp_path)
    assert "Password" in c.get("/login").text
    bad = form(c, "/login", password="wrong", next="/")
    assert bad.status_code == 401 and "Wrong password" in bad.text and "set-cookie" not in bad.headers
    assert PASSWORD not in bad.text
    ok = form(c, "/login", password=PASSWORD, next="/status")
    assert ok.status_code == 303 and ok.headers["location"] == "/status"
    cookie = ok.headers["set-cookie"]
    assert cookie.startswith("sounding_session=")
    for attr in ("HttpOnly", "SameSite=lax", "Path=/", "Secure", "Max-Age=2592000"):
        assert attr.lower() in cookie.lower(), attr
    assert c.get("/status").status_code == 200  # TestClient keeps the cookie
    assert c.get("/api/status").status_code == 200
    out = c.post("/logout")
    assert out.status_code == 303 and out.headers["location"] == "/login"
    assert c.get("/status").status_code == 303  # cookie cleared


def test_cookie_not_secure_for_http_base_url(tmp_path):
    c = client(tmp_path, public_base_url="http://localhost:8080")
    app_client = TestClient(c.app, base_url="http://localhost:8080", follow_redirects=False)
    r = form(app_client, "/login", password=PASSWORD, next="/")
    assert r.status_code == 303 and "secure" not in r.headers["set-cookie"].lower()


def test_tampered_and_expired_cookies_are_rejected(seeded, tmp_path):
    c = client(tmp_path)
    good = auth.make_session(SECRET)
    c.cookies.set("sounding_session", good)
    assert c.get("/api/status").status_code == 200
    payload, mac = good.split(".")
    for bad in (
        payload + "." + mac[:-2] + ("AA" if not mac.endswith("AA") else "BB"),      # wrong MAC
        auth.make_session("another-secret-another-secret-another-secret"),           # signed with another secret
        auth.make_session(SECRET, now=datetime.now(UTC).timestamp() - 40 * 86400),   # expired
        auth.make_session(SECRET, now=datetime.now(UTC).timestamp() + 86400),        # issued in the future
        payload, "", "a.b.c", "." + mac,
    ):
        c.cookies.clear()
        c.cookies.set("sounding_session", bad)
        assert c.get("/api/status").status_code == 401, bad
    # a forged payload with a valid structure but the old MAC
    import base64, json

    forged = base64.urlsafe_b64encode(json.dumps({"iat": 0, "exp": 9999999999}).encode()).decode().rstrip("=")
    c.cookies.clear()
    c.cookies.set("sounding_session", forged + "." + mac)
    assert c.get("/api/status").status_code == 401


def test_session_unit():
    s = auth.make_session(SECRET, now=1000, lifetime=60)
    assert auth.verify_session(SECRET, s, now=1030)
    assert not auth.verify_session(SECRET, s, now=1060)
    assert not auth.verify_session(SECRET, None)
    assert not auth.verify_session(SECRET, "x")


@pytest.mark.parametrize("value,expected", [
    ("/status", "/status"), ("/digests?page=2", "/digests?page=2"), ("/", "/"), ("", "/"), (None, "/"),
    ("//evil.example.net", "/"), ("//evil.example.net/x", "/"), ("/\\evil.example.net", "/"), ("\\\\evil.example.net", "/"),
    ("https://evil.example.net", "/"), ("http://evil.example.net/", "/"), ("javascript:alert(1)", "/"), ("evil.example.net", "/"),
    ("/x\r\nSet-Cookie: a=b", "/"), ("/\tx", "/"),
])
def test_safe_next(value, expected):
    assert safe_next(value) == expected


def test_login_redirects_only_to_same_site(tmp_path):
    c = client(tmp_path)
    for nxt in ("//evil.example.net", "https://evil.example.net", "/\\evil.example.net"):
        r = form(c, "/login", password=PASSWORD, next=nxt)
        assert r.status_code == 303 and r.headers["location"] == "/", nxt
    c2 = client(tmp_path, login=True)
    assert c2.get("/login?next=//evil.example.net").headers["location"] == "/"


def test_login_throttle(tmp_path):
    now = [1000.0]
    throttle = auth.LoginThrottle(clock=lambda: now[0])
    app = create_app(make_settings(tmp_path), get_session_factory(), throttle=throttle)
    c = TestClient(app, base_url=BASE, follow_redirects=False)
    for _ in range(5):
        assert form(c, "/login", password="nope").status_code == 401
    r = form(c, "/login", password=PASSWORD)  # even the right password is refused while throttled
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1 and "set-cookie" not in r.headers
    assert PASSWORD not in r.text
    now[0] += 3  # 2 seconds is the first delay
    assert form(c, "/login", password="nope").status_code == 401  # 6th failure: next delay is 4 seconds
    assert form(c, "/login", password=PASSWORD).status_code == 429
    now[0] += 5
    assert form(c, "/login", password=PASSWORD).status_code == 303  # allowed again, and a success resets the counter
    assert form(c, "/login", password="nope").status_code == 401


def test_throttle_unit():
    now = [0.0]
    t = auth.LoginThrottle(clock=lambda: now[0])
    for _ in range(4):
        t.failure("a")
    assert t.retry_after("a") == 0
    t.failure("a")
    assert t.retry_after("a") == 2 and t.retry_after("b") == 0  # per address
    now[0] = 2.0
    assert t.retry_after("a") == 0
    for _ in range(20):
        t.failure("a")
    assert t.retry_after("a") == 900  # capped
    now[0] = 2000.0  # the window has passed
    assert t.retry_after("a") == 0
    small = auth.LoginThrottle(clock=lambda: now[0], max_entries=3)
    for k in "wxyz":
        small.failure(k)
    assert len(small._fails) <= 3


def test_login_refuses_cross_origin_post(tmp_path):
    c = client(tmp_path)
    r = c.post("/login", content=f"password={PASSWORD}", headers={"content-type": "application/x-www-form-urlencoded", "origin": "https://evil.example.net"})
    assert r.status_code == 403 and "set-cookie" not in r.headers
    ok = c.post("/login", content=f"password={PASSWORD}", headers={"content-type": "application/x-www-form-urlencoded", "origin": BASE})
    assert ok.status_code == 303
    # reached by another name: the Origin matches the Host header
    same = c.post("/login", content=f"password={PASSWORD}", headers={"content-type": "application/x-www-form-urlencoded", "origin": "http://localhost:9999", "host": "localhost:9999"})
    assert same.status_code == 303
    out = c.post("/logout", headers={"origin": "https://evil.example.net"})
    assert out.status_code == 403


def test_login_body_limit_and_garbage(tmp_path):
    c = client(tmp_path)
    assert c.post("/login", content="x" * 5000, headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 413
    assert form(c, "/login").status_code == 401
    assert c.post("/login", content=b"\xff\xfe=%zz", headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 401


def test_password_hash_unit():
    h = auth.hash_password("pw", n=2**4)
    assert h.startswith("scrypt$16$8$1$") and h.count("$") == 5
    assert auth.verify_password("pw", h) and not auth.verify_password("pw2", h) and not auth.verify_password("pw", "garbage")
    assert auth.hash_password("pw", n=2**4) != h  # random salt
    assert not auth.verify_password("pw", "scrypt$99999999$8$1$YQ$YQ")  # out of range: refused, not computed


# --- no password -----------------------------------------------------------------------------------

def test_empty_password_mode(seeded, tmp_path, caplog):
    with caplog.at_level("WARNING", logger="sounding.web"):
        c = client(tmp_path, web_password_hash="")
    assert any("NO login" in r.message for r in caplog.records)
    for path in all_pages(seeded):
        r = c.get(path)
        assert r.status_code == 200, path
        if not path.startswith("/api"):
            assert 'class="banner"' in r.text and "No password is set" in r.text, path
    assert "Log out" not in c.get("/").text
    assert c.get("/login").headers["location"] == "/"
    assert c.post("/login", content="password=x").status_code == 303
    assert c.post("/logout").headers["location"] == "/"


def test_banner_absent_with_password(seeded, tmp_path):
    assert "No password is set" not in client(tmp_path, login=True).get("/").text
    assert "Log out" in client(tmp_path, login=True).get("/").text


# --- startup validation ------------------------------------------------------------------------------

@pytest.mark.parametrize("secret", ["", "short", "x" * 31, "change-me-session-secret", "a" * 40 + "change-me"])
def test_session_secret_validation_refuses_to_start(secret, tmp_path):
    with pytest.raises(auth.WebConfigError) as exc:
        create_app(make_settings(tmp_path, session_secret=secret), get_session_factory())
    if secret:
        assert secret not in str(exc.value) or secret == "short"  # the message names the key, not the value


def test_malformed_password_hash_refuses_to_start(tmp_path):
    with pytest.raises(auth.WebConfigError) as exc:
        create_app(make_settings(tmp_path, web_password_hash="scrypt$32768$8$1"), get_session_factory())
    assert "$$" in str(exc.value)  # points at the .env escaping


def test_main_exits_nonzero_on_bad_secret(monkeypatch, capsys, tmp_path):
    import sounding.config
    from sounding.web.__main__ import main

    monkeypatch.setattr(sounding.config, "get_settings", lambda: make_settings(tmp_path, session_secret="change-me-session-secret"))
    called = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: called.append((a, k)))
    assert main() == 1
    err = capsys.readouterr().err
    assert "refusing to start" in err and "SESSION_SECRET" in err and not called


def test_main_runs_uvicorn_on_8080(monkeypatch, tmp_path):
    import sounding.config
    from sounding.web.__main__ import main

    monkeypatch.setattr(sounding.config, "get_settings", lambda: make_settings(tmp_path))
    called = []
    monkeypatch.setattr("uvicorn.run", lambda app, **k: called.append(k))
    assert main() == 0
    assert called[0]["host"] == "0.0.0.0" and called[0]["port"] == 8080 and called[0]["access_log"] is False


# --- hash-password CLI ---------------------------------------------------------------------------------

def test_hash_cli_stdin_round_trip(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("my pass phrase\n"))
    assert cli_main(["hash-password", "--stdin"]) == 0
    out = capsys.readouterr()
    h = out.out.strip()
    assert h.startswith("scrypt$32768$8$1$") and out.out.count("\n") == 1 and "my pass phrase" not in out.out + out.err
    assert auth.verify_password("my pass phrase", h) and not auth.verify_password("other", h)
    auth.validate_web_config(SECRET, h)


def test_hash_cli_getpass(monkeypatch, capsys):
    answers = iter(["s3cret-pass", "s3cret-pass"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert cli_main(["hash-password"]) == 0
    h = capsys.readouterr().out.strip()
    assert auth.verify_password("s3cret-pass", h)


def test_hash_cli_mismatch_and_empty(monkeypatch, capsys):
    answers = iter(["one", "two"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert cli_main(["hash-password"]) == 1
    cap = capsys.readouterr()
    assert cap.out == "" and "differ" in cap.err
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert cli_main(["hash-password", "--stdin"]) == 1
    assert capsys.readouterr().out == ""


# --- Atom ----------------------------------------------------------------------------------------------

def test_atom_feed(seeded, tmp_path, session):
    c = client(tmp_path)  # no session: the feed authenticates by its token
    token = auth.feed_token(SECRET)
    assert len(token) == 32 and re.fullmatch(r"[0-9a-f]{32}", token)
    r = c.get(f"/feed/{token}.atom")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/atom+xml")
    feed = feedparser.parse(r.content)
    assert not feed.bozo and feed.feed.title == "Sounding"
    assert len(feed.entries) == 1
    e = feed.entries[0]
    assert e.link == f"{BASE}/digests/{seeded['digest']}"
    assert e.id.startswith("tag:digest.example.com,") and e.id.endswith(f":digest:{seeded['digest']}")
    assert "2026-10-01 07:00" in e.title and e.updated.startswith("2026-10-02T03:00:00")
    text = e.content[0].value
    assert "Authors converge on option B (v2)" in text and f"{BASE}/conversations/{seeded['conv']}" in text and "An article <b>bold</b>" in text
    assert c.get("/feed/0123456789abcdef0123456789abcdef.atom").status_code == 404
    assert c.get(f"/feed/{token[:-1]}0.atom").status_code == 404
    assert c.get("/feed/.atom").status_code == 404


def test_atom_limits_to_20_and_skips_unfinished(session, tmp_path):
    body = {"period": {"start": "2026-10-01T07:00+09:00", "end": "2026-10-02T07:00+09:00", "tz": "Asia/Tokyo"}, "language": "en", "quiet": True,
            "health": [], "conversations": [], "events": [], "items": [], "counts": {}}
    for i in range(25):
        session.add(Digest(period_start=END + timedelta(days=i), period_end=END + timedelta(days=i + 1), language="en", body_json=body))
    session.add(Digest(period_start=END + timedelta(days=99), period_end=END + timedelta(days=100), language="en", body_json={}))
    session.commit()
    feed = feedparser.parse(client(tmp_path).get(f"/feed/{auth.feed_token(SECRET)}.atom").content)
    assert len(feed.entries) == 20 and len({e.id for e in feed.entries}) == 20


def test_atom_empty_feed_is_valid(session, tmp_path):
    r = client(tmp_path).get(f"/feed/{auth.feed_token(SECRET)}.atom")
    assert r.status_code == 200 and feedparser.parse(r.content).entries == []


def test_atom_token_depends_on_secret():
    assert auth.feed_token(SECRET) != auth.feed_token(SECRET + "x")
    assert auth.feed_token("k" * 32) == auth.feed_token("k" * 32)


# --- compose --------------------------------------------------------------------------------------------

FORBIDDEN = ("LLM_API_KEY", "IDENTITY_SECRET", "SMTP_PASSWORD", "SMTP_USER", "WEBHOOK_URL")


def test_compose_web_gets_no_worker_secrets():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    web = compose["services"]["sounding-web"]
    assert "env_file" not in web, "web must not read the whole env file"
    assert "profiles" not in web, "web starts by default"
    env = web["environment"]
    assert isinstance(env, dict)
    for key in FORBIDDEN:
        assert key not in env, key
    assert not any(k.startswith(("SMTP_", "LLM_API", "IDENTITY", "WEBHOOK")) for k in env)
    for key in ("SESSION_SECRET", "WEB_PASSWORD_HASH", "PUBLIC_BASE_URL", "DATABASE_URL", "PROFILE_PATH"):
        assert key in env, key
    assert web["command"] == ["python", "-m", "sounding.web"]
    assert "/healthz" in " ".join(web["healthcheck"]["test"])
    assert "./config:/app/config:ro" in web["volumes"]
    # the worker is the one that gets everything
    assert compose["services"]["sounding-worker"]["env_file"] == ".env"
    anchor_env = compose["x-app-env"]
    assert not set(FORBIDDEN) & set(anchor_env)
