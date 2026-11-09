"""Digest period, body, text rendering, run_digest, CLI and worker job (spec phase 3, R5/R11): real Postgres, FakeProvider."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select
from test_select import mksrc, msg, post, prof

from sounding import jobs
from sounding.cli import format_status, main
from sounding.config import Settings, get_settings
from sounding.db import get_engine, get_session_factory
from sounding.digest import EVENT_LABELS, build_digest, latest_fire_time, render_digest_text
from sounding.llm import FakeProvider, LLMClient
from sounding.models import ConversationState, Delivery, Digest, Event, JobRun, Selection
from sounding.profile import Profile
from sounding.worker import Worker, build_scheduler, load_start_profile

DB_URL = "postgresql+psycopg://dbuser:db-pass-9d2e@localhost:1/none"
ADDR = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# 2026-10-02 12:00 JST. The 07:00 JST schedule last fired at 2026-10-01 22:00 UTC; test_select's T0 (10-01 12:00 UTC) is inside.
NOW = datetime(2026, 10, 2, 3, 0, tzinfo=UTC)
END = datetime(2026, 10, 1, 22, 0, tzinfo=UTC)


def settings(**kw) -> Settings:
    base = dict(
        database_url=DB_URL, llm_api_key="sk-test-llm-key-5c1b", llm_provider="none",
        profile_path="/nonexistent/profile.yaml", tz="Asia/Tokyo", public_base_url="https://digest.example.com/",
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


@pytest.fixture
def sf(session):
    return get_session_factory()


def client_for(sf, script=None):
    fake = FakeProvider(script or (lambda req: {"fits": True, "reason": "ok"} if req.purpose == "rss_select" else {"summary": "Short summary."}))
    return LLMClient(fake, sf, tz="Asia/Tokyo", daily_budget_usd=Decimal("0"), default_max_tokens=500)


def build(sf, profile=None, client=None, now=NOW, **kw):
    return build_digest(sf, settings(**kw), client=client, profile=profile or prof(), now=now)


def add_state(session, cid, *, version=1, language="en", status_line="WGLC in progress", changed="Scope was agreed"):
    session.add(ConversationState(
        conversation_id=cid, language=language, version=version, state_json={"status_line": status_line, "issues": []},
        changed_since_last=changed, covered_item_ids=[], last_item_published_at=END, prompt_version="t", model="fake",
    ))
    session.commit()


def add_event(session, src, item, kind, ref, hours=0.0):
    session.add(Event(
        source_id=src.id, item_id=item.id, conversation_id=item.conversation_id, kind=kind, ref=ref,
        occurred_at=item.published_at + timedelta(hours=hours),
    ))
    session.commit()


# --- period -----------------------------------------------------------------------------------------

def test_latest_fire_time_in_tz_and_inclusive():
    # 07:00 JST: before today's time -> yesterday's; at it -> itself (inclusive)
    assert latest_fire_time("0 7 * * *", "Asia/Tokyo", NOW).isoformat() == "2026-10-02T07:00:00+09:00"
    assert latest_fire_time("0 7 * * *", "Asia/Tokyo", NOW).astimezone(UTC) == END
    assert latest_fire_time("0 7 * * *", "Asia/Tokyo", END).astimezone(UTC) == END
    assert latest_fire_time("0 7 * * *", "Asia/Tokyo", END - timedelta(minutes=1)).astimezone(UTC) == END - timedelta(days=1)
    # the same cron string means a different instant in UTC
    assert latest_fire_time("0 7 * * *", "UTC", NOW).astimezone(UTC) == datetime(2026, 10, 2, 7, tzinfo=UTC) - timedelta(days=1)
    # weekly: looks back up to 8 days
    wk = latest_fire_time("0 7 * * mon", "UTC", datetime(2026, 10, 7, 12, tzinfo=UTC))
    assert wk == datetime(2026, 10, 5, 7, tzinfo=UTC)
    with pytest.raises(ValueError):
        latest_fire_time("not a cron", "UTC", NOW)


def test_first_digest_covers_24h_and_next_starts_at_previous_end(session, sf):
    r1 = build(sf)
    assert r1.period_end == END and r1.period_start == END - timedelta(hours=24) and not r1.rebuilt
    r2 = build(sf, now=NOW + timedelta(days=1))
    assert r2.digest_id != r1.digest_id and not r2.rebuilt
    assert r2.period_start == END and r2.period_end == END + timedelta(days=1)
    # a gap of days: the period simply spans it
    r3 = build(sf, now=NOW + timedelta(days=4))
    assert r3.period_start == END + timedelta(days=1) and r3.period_end == END + timedelta(days=4)
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Digest)) == 3


def test_body_period_is_in_tz(session, sf):
    body = build(sf).body
    assert body["period"] == {"start": "2026-10-01T07:00+09:00", "end": "2026-10-02T07:00+09:00", "tz": "Asia/Tokyo"}


def test_rebuild_in_place_keeps_id_and_deliveries(session, sf):
    wg = mksrc(session, "ietf_mail", "sidrops")
    first = build(sf)
    assert first.body["conversations"] == []
    session.add(Delivery(digest_id=first.digest_id, channel="email", status="sent", attempts=1, last_attempt_at=NOW))
    session.commit()
    created_at = session.get(Digest, first.digest_id).created_at
    msg(session, wg, "[sidrops] ASPA")  # activity appears; a manual run before the next fire time rebuilds it
    second = build(sf, now=NOW + timedelta(hours=5))
    assert second.rebuilt and second.digest_id == first.digest_id and second.period_start == first.period_start
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Digest)) == 1
    d = session.get(Digest, first.digest_id)
    assert [c["subject"] for c in d.body_json["conversations"]] == ["[sidrops] ASPA"]
    assert d.created_at == created_at
    deliveries = session.scalars(select(Delivery)).all()
    assert len(deliveries) == 1 and deliveries[0].status == "sent" and deliveries[0].attempts == 1
    # selections were recomputed, not duplicated
    assert session.scalar(select(func.count()).select_from(Selection).where(Selection.digest_id == d.id)) == 1


def test_failed_build_leaves_no_new_row(session, sf, monkeypatch):
    from sounding import digest as dg

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(dg, "select_for_digest", boom)
    with pytest.raises(RuntimeError):
        build(sf)
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Digest)) == 0


# --- body -------------------------------------------------------------------------------------------

def test_conversation_with_and_without_state(session, sf):
    wg = mksrc(session, "ietf_mail", "sidrops")
    a = msg(session, wg, "[sidrops] ASPA verification", author="Alice Example", hours=1)
    msg(session, wg, "Re: [sidrops] ASPA verification", author="Bob Example", hours=2, irt=a.message_id)
    b = msg(session, wg, "[sidrops] Other topic", author="Carol", hours=3)
    add_state(session, a.conversation_id, version=1, status_line="old line")
    add_state(session, a.conversation_id, version=2)
    add_state(session, b.conversation_id, language="ja", status_line="日本語")  # wrong language: ignored
    body = build(sf).body
    by_id = {c["id"]: c for c in body["conversations"]}
    ca, cb = by_id[a.conversation_id], by_id[b.conversation_id]
    assert ca["state"] == {"version": 2, "status_line": "WGLC in progress", "changed_since_last": "Scope was agreed"}
    assert ca["new_messages"] == 2 and ca["participants"] == ["Alice Example", "Bob Example"]
    assert ca["list"] == "sidrops" and ca["score"] > cb["score"]
    assert body["conversations"][0]["id"] == a.conversation_id  # best first
    assert ca["web_url"] == f"https://digest.example.com/conversations/{a.conversation_id}"
    assert ca["latest_url"].startswith("https://mailarchive.example.com/") and ca["latest_url"] != by_id[b.conversation_id]["latest_url"]
    assert cb["state"] is None and cb["new_messages"] == 1
    assert body["counts"]["conversations"] == 2 and body["quiet"] is False


def test_llm_none_has_health_entry_and_still_lists_items_by_rule(session, sf):
    feed = mksrc(session, "rss", "https://blog.example.com/feed")
    post(session, feed, "RPKI deployment report")
    post(session, feed, "Something borderline")  # needs the LLM; none -> excluded
    body = build(sf, profile=prof(keywords={"include": ["rpki"]}), client=None).body
    assert [h["kind"] for h in body["health"]] == ["llm_disabled"]
    assert [i["title"] for i in body["items"]] == ["RPKI deployment report"]
    assert body["items"][0]["summary"] is None and body["items"][0]["feed"] == "https://blog.example.com/feed"
    assert body["counts"]["items_with_summary"] == 0


def test_item_summary_present_with_client(session, sf):
    feed = mksrc(session, "rss", "https://blog.example.com/feed")
    post(session, feed, "RPKI deployment report")
    body = build(sf, profile=prof(keywords={"include": ["rpki"]}), client=client_for(sf)).body
    assert body["health"] == []
    assert body["items"][0]["summary"] == "Short summary." and body["items"][0]["url"].startswith("https://blog.example.com/")
    assert body["counts"]["items_with_summary"] == 1


def test_health_failed_jobs_and_budget(session, sf):
    inside, outside = END - timedelta(hours=5), END - timedelta(days=3)
    for t in (inside, inside, outside):
        session.add(JobRun(job="fetch", started_at=t, status="failed", error="fetch_failed"))
    session.add(JobRun(job="fetch", started_at=inside, status="ok"))
    session.add(JobRun(job="summarize", started_at=inside, status="ok", counts={"budget_skipped": 3}))
    session.add(JobRun(job="summarize", started_at=inside, status="ok", counts={"budget_skipped": 2}))
    session.commit()
    health = build(sf, client=client_for(sf)).body["health"]
    assert health == [
        {"kind": "job_failed", "detail": "fetch: fetch_failed x2"},
        {"kind": "budget_skipped", "detail": "5"},
    ]
    assert build(sf, client=client_for(sf), now=NOW + timedelta(days=1)).body["health"] == []


def test_events_only_for_tracked_items(session, sf):
    wg = mksrc(session, "ietf_mail", "sidrops")
    other = mksrc(session, "ietf_mail", "opsec")
    e1 = msg(session, wg, "Last Call: draft-ietf-sidrops-aspa-verification-19", auto=True, hours=1)
    e2 = msg(session, other, "I-D Action: draft-ietf-sidrops-rov-00.txt", auto=True, hours=2)  # tracked by draft prefix
    e3 = msg(session, other, "I-D Action: draft-someone-else-00.txt", auto=True, hours=3)  # not tracked
    e4 = msg(session, wg, "I-D Action: draft-old-00", auto=True, hours=-40)  # tracked list but before the period
    e5 = msg(session, wg, "Protocol Action: RFC 9999", auto=True, hours=4)  # tracked list, no draft ref
    add_event(session, wg, e1, "ietf.last_call", "draft-ietf-sidrops-aspa-verification-19")
    add_event(session, other, e2, "ietf.id_action", "draft-ietf-sidrops-rov-00")
    add_event(session, other, e3, "ietf.id_action", "draft-someone-else-00")
    add_event(session, wg, e4, "ietf.id_action", "draft-old-00")
    add_event(session, wg, e5, "ietf.publication", None)
    body = build(sf).body
    assert [(e["kind"], e["ref"]) for e in body["events"]] == [
        ("ietf.last_call", "draft-ietf-sidrops-aspa-verification-19"),
        ("ietf.id_action", "draft-ietf-sidrops-rov-00"),
        ("ietf.publication", None),
    ]
    assert body["events"][0]["label"] == "Last Call" and body["events"][0]["url"] == e1.url and body["events"][0]["list"] == "sidrops"
    assert body["counts"]["events"] == 3 and body["quiet"] is False
    ja = build(sf, profile=Profile.model_validate({**prof().model_dump(), "profile": {"language": "ja"}}), now=NOW).body
    assert ja["language"] == "ja" and [e["label"] for e in ja["events"]][0] == "Last Call"
    assert ja["events"][1]["label"] == EVENT_LABELS["ja"]["ietf.id_action"]


def test_quiet_day_is_a_digest(session, sf):
    body = build(sf, client=client_for(sf)).body
    assert body["quiet"] is True and body["conversations"] == [] and body["items"] == [] and body["events"] == []
    assert "No activity" in render_digest_text(body, "en") and "動きがありませんでした" in render_digest_text(body, "ja")
    row = session.scalar(select(Digest))
    assert row.body_json["quiet"] is True


def test_no_body_text_or_address_in_body_json(session, sf):
    wg = mksrc(session, "ietf_mail", "sidrops")
    sentence = "The zebra protocol must never leak into any digest."
    a = msg(session, wg, "[sidrops] Question from carol@example.com", body=f"{sentence} Write to carol@example.com", author="Carol <carol@example.com>")
    add_state(session, a.conversation_id, status_line="Carol (carol@example.com) objects")
    feed = mksrc(session, "rss", "https://blog.example.com/feed")
    post(session, feed, "RPKI news", body=f"{sentence} mail me at dave@example.com")
    body = build(sf, profile=prof(keywords={"include": ["rpki"]}), client=client_for(sf)).body
    stored = json.dumps(session.scalar(select(Digest)).body_json, ensure_ascii=False)
    assert body["conversations"] and body["items"]
    assert "zebra" not in stored and not ADDR.findall(stored)
    text = render_digest_text(body, "en")
    assert "zebra" not in text and not ADDR.findall(text)


# --- text -------------------------------------------------------------------------------------------

def _full_body(session, sf):
    wg = mksrc(session, "ietf_mail", "sidrops")
    a = msg(session, wg, "[sidrops] ASPA verification", author="Alice", hours=1)
    msg(session, wg, "[sidrops] No summary yet", author="Bob", hours=2)
    add_state(session, a.conversation_id)
    ev = msg(session, wg, "Last Call: draft-ietf-sidrops-aspa-verification-19", auto=True, hours=3)
    add_event(session, wg, ev, "ietf.last_call", "draft-ietf-sidrops-aspa-verification-19")
    feed = mksrc(session, "rss", "https://blog.example.com/feed")
    post(session, feed, "RPKI deployment report")
    session.add(JobRun(job="fetch", started_at=END - timedelta(hours=2), status="failed", error="fetch_failed"))
    session.commit()
    return build(sf, profile=prof(keywords={"include": ["rpki"]}), client=client_for(sf)).body, a


def test_render_text_en_and_ja(session, sf):
    body, a = _full_body(session, sf)
    en = render_digest_text(body, "en")
    for needle in (
        "Sounding digest", "[Health]", "Job failed: fetch: fetch_failed", "[Active conversations]",
        "[sidrops] ASPA verification", "WGLC in progress", "Changed since last: Scope was agreed", "1 new message)",
        f"https://digest.example.com/conversations/{a.conversation_id}", "Latest message: https://mailarchive.example.com/",
        "(no summary yet)", "[Procedural updates]", "Last Call draft-ietf-sidrops-aspa-verification-19",
        "[Related articles]", "RPKI deployment report", "Short summary.",
    ):
        assert needle in en, needle
    ja = render_digest_text(body, "ja")
    for needle in ("Sounding ダイジェスト", "[健康状態]", "ジョブが失敗", "[動きがあった会話]", "前回から: Scope was agreed", "新着 1 件", "詳細:", "最新のメッセージ:", "（まだ要約なし）", "[手続きの更新]", "[関連記事]"):
        assert needle in ja, needle
    assert "[Health]" not in ja and render_digest_text(body, "fr") == en  # unknown language -> en


def test_render_text_omits_empty_sections(session, sf):
    text = render_digest_text(build(sf, client=client_for(sf)).body, "en")
    assert "[Health]" not in text and "[Active" not in text and "[Procedural" not in text and "[Related" not in text


# --- job --------------------------------------------------------------------------------------------

def test_run_digest_writes_job_run_and_delivery_hook(session, sf, caplog):
    caplog.set_level(logging.DEBUG)
    rep = jobs.run_digest(sf, settings(), profile=prof(), now=NOW)
    assert rep.status == "ok" and rep.delivery == jobs.NO_CHANNELS and "delivery skipped" in caplog.text
    session.expire_all()
    run = session.scalars(select(JobRun).where(JobRun.job == "digest")).one()
    assert run.status == "ok" and run.source_id is None
    assert run.counts["digest_id"] == rep.digest_id and run.counts["rebuilt"] is False and run.counts["conversations"] == 0
    assert run.counts["delivery"] == jobs.NO_CHANNELS
    rep2 = jobs.run_digest(sf, settings(), profile=prof(), deliver=False, now=NOW)
    assert rep2.digest_id == rep.digest_id and rep2.rebuilt and rep2.delivery is None
    session.expire_all()
    last = session.scalars(select(JobRun).where(JobRun.job == "digest").order_by(JobRun.id.desc())).first()
    assert "delivery" not in last.counts and last.counts["rebuilt"] is True


def test_run_digest_config_error_fails_and_unexpected_error_is_recorded(session, sf, monkeypatch):
    rep = jobs.run_digest(sf, settings(llm_provider="bogus"), profile=prof(), now=NOW)
    assert rep.status == "failed" and rep.reason == "config_error" and rep.digest_id is None
    session.expire_all()
    run = session.scalars(select(JobRun).where(JobRun.job == "digest")).one()
    assert run.status == "failed" and run.error == "config_error"
    assert session.scalar(select(func.count()).select_from(Digest)) == 0

    from sounding import digest as dg

    monkeypatch.setattr(dg, "select_for_digest", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret detail")))
    with pytest.raises(RuntimeError):
        jobs.run_digest(sf, settings(), profile=prof(), now=NOW)
    session.expire_all()
    last = session.scalars(select(JobRun).where(JobRun.job == "digest").order_by(JobRun.id.desc())).first()
    assert last.status == "failed" and last.error == "RuntimeError" and "secret" not in json.dumps(last.counts or {})


def test_settings_public_base_url_strips_slash_and_defaults():
    assert settings(public_base_url="https://digest.example.com//").public_base_url == "https://digest.example.com"
    assert Settings(_env_file=None, database_url=DB_URL).public_base_url == "http://localhost:8080"


# --- CLI --------------------------------------------------------------------------------------------

def test_cli_digest_show_digest_and_status(session, sf, tmp_path, monkeypatch, capsys):
    profile = tmp_path / "profile.yaml"
    profile.write_text("ietf:\n  working_groups: [sidrops]\nprofile:\n  language: en\n")
    monkeypatch.setenv("PROFILE_PATH", str(profile))
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://digest.example.com")
    get_settings.cache_clear()
    try:
        assert main(["show-digest"]) == 0
        assert "no digest yet" in capsys.readouterr().out
        assert "last digest: (never)" in format_status(session, settings())
        assert main(["digest"]) == 2
        capsys.readouterr()
        assert main(["digest", "--once", "--no-deliver"]) == 0
        out = capsys.readouterr().out
        assert "digest 1 (created) counts=" in out and "delivery:" not in out
        assert "No activity" in out and "LLM is disabled" in out
        assert main(["digest", "--once"]) == 0
        out = capsys.readouterr().out
        assert "digest 1 (rebuilt)" in out and f"delivery: {jobs.NO_CHANNELS}" in out
        assert main(["show-digest"]) == 0
        shown = capsys.readouterr().out
        assert shown.startswith("digest 1 language=en") and "Sounding digest" in shown and "No activity" in shown
        assert main(["show-digest", "1"]) == 0
        assert "Sounding digest" in capsys.readouterr().out
        assert main(["show-digest", "99"]) == 0
        assert "no such digest" in capsys.readouterr().out
        session.expire_all()
        assert "last digest: id=1 period=" in format_status(session, settings()) and "conversations=0 items=0" in format_status(session, settings())
        profile.write_text("bogus: 1\n")
        assert main(["digest", "--once"]) == 1
        assert "profile error" in capsys.readouterr().err
    finally:
        get_settings.cache_clear()


# --- worker -----------------------------------------------------------------------------------------

def test_worker_registers_digest_job_on_profile_schedule(infra):
    st = settings()
    profile = Profile.model_validate({"profile": {"digest": {"schedule": "30 6 * * *"}}})
    worker = Worker(st, get_session_factory(), get_engine(), profile)
    jobs_ = {j.id: j for j in build_scheduler(worker, now=NOW).get_jobs()}
    assert set(jobs_) == {"fetch", "retention", "summarize", "digest"}
    d = jobs_["digest"]
    assert isinstance(d.trigger, CronTrigger) and d.max_instances == 1 and d.func == worker.digest_job
    fields = {f.name: str(f) for f in d.trigger.fields}
    assert fields["hour"] == "6" and fields["minute"] == "30" and str(d.trigger.timezone) == "Asia/Tokyo"
    assert "digest" in jobs.LOCK_KEYS and len(set(jobs.LOCK_KEYS.values())) == len(jobs.LOCK_KEYS)


def test_worker_survives_invalid_profile_or_schedule(infra, tmp_path, caplog):
    bad = tmp_path / "profile.yaml"
    bad.write_text("bogus: 1\n")
    with caplog.at_level(logging.ERROR):
        assert load_start_profile(settings(profile_path=str(bad))) is None
    assert "digest job is not registered" in caplog.text
    worker = Worker(settings(), get_session_factory(), get_engine(), None)
    assert "digest" not in {j.id for j in build_scheduler(worker, now=NOW).get_jobs()}
    weird = Profile.model_validate({"profile": {"digest": {"schedule": "every day please"}}})
    worker = Worker(settings(), get_session_factory(), get_engine(), weird)
    with caplog.at_level(logging.ERROR):
        ids = {j.id for j in build_scheduler(worker, now=NOW).get_jobs()}
    assert ids == {"fetch", "retention", "summarize"} and "not a valid cron string" in caplog.text


def test_worker_digest_job_runs_locks_and_skips(session, sf):
    worker = Worker(settings(), sf, get_engine(), Profile())
    worker.digest_job()
    session.expire_all()
    run = session.scalars(select(JobRun).where(JobRun.job == "digest")).one()
    assert run.status == "ok" and run.counts["digest_id"]
    with jobs.advisory_lock(get_engine(), "digest") as got:
        assert got
        worker.digest_job()
    session.expire_all()
    runs = session.scalars(select(JobRun).where(JobRun.job == "digest").order_by(JobRun.id)).all()
    assert [r.status for r in runs] == ["ok", "skipped"] and runs[1].error == "locked"
