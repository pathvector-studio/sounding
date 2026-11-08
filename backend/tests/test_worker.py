"""Retention, advisory locks, the stop flag and the worker's scheduler setup (real Postgres)."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from sounding.config import Settings
from sounding.db import get_engine, get_session_factory
from sounding.jobs import advisory_lock, record_skipped, run_fetch, run_retention
from sounding.models import Item, JobRun
from sounding.profile import Feed, Ietf, Profile
from sounding.sources.rss import RssAdapter
from sounding.worker import Worker, build_scheduler, startup_summary

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
CONTACT = "contact-7f3a@example.net"
SECRET = "unit-test-identity-secret-0123456789"
DB_URL = "postgresql+psycopg://dbuser:db-pass-9d2e@localhost:1/none"


def make_settings(**kw) -> Settings:
    base = dict(
        database_url=DB_URL, fetch_contact=CONTACT, identity_secret=SECRET,
        llm_api_key="sk-test-llm-key-5c1b", profile_path="/nonexistent/profile.yaml",
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


def add_source(session):
    from sounding.models import Source

    s = Source(kind="rss", key="https://example.com/feed.xml")
    session.add(s)
    session.flush()
    return s


def test_retention_clears_only_old_bodies(session):
    src = add_source(session)
    ages = {"old": 91, "edge_new": 89, "fresh": 1}
    for name, age in ages.items():
        session.add(Item(
            source_id=src.id, external_id=name, title=name, published_at=NOW, body_text="body " + name,
            body_hash="hash-" + name, url="https://example.com/" + name, fetched_at=NOW - timedelta(days=age),
        ))
    session.commit()
    counts = run_retention(get_session_factory(), make_settings(content_retention_days=90), now=NOW)
    assert counts == {"cleared": 1}
    session.expire_all()
    rows = {i.external_id: i for i in session.scalars(select(Item))}
    assert rows["old"].body_text is None
    assert rows["old"].body_hash == "hash-old" and rows["old"].title == "old"
    assert rows["old"].url == "https://example.com/old"
    assert rows["edge_new"].body_text and rows["fresh"].body_text
    run = session.scalars(select(JobRun)).one()
    assert run.job == "retention" and run.status == "ok" and run.counts == {"cleared": 1}
    # idempotent: nothing left to clear
    assert run_retention(get_session_factory(), make_settings(content_retention_days=90), now=NOW) == {"cleared": 0}


def test_retention_zero_disables(session):
    src = add_source(session)
    session.add(Item(
        source_id=src.id, external_id="x", title="x", published_at=NOW, body_text="keep me",
        fetched_at=NOW - timedelta(days=3650),
    ))
    session.commit()
    counts = run_retention(get_session_factory(), make_settings(content_retention_days=0), now=NOW)
    assert counts["cleared"] == 0
    session.expire_all()
    assert session.scalars(select(Item)).one().body_text == "keep me"
    assert session.scalars(select(JobRun)).one().job == "retention"


def test_advisory_lock_is_exclusive_per_job(infra):
    engine = get_engine()
    with advisory_lock(engine, "fetch") as first:
        assert first is True
        with advisory_lock(engine, "fetch") as second:
            assert second is False
        with advisory_lock(engine, "retention") as other_job:
            assert other_job is True
    with advisory_lock(engine, "fetch") as again:
        assert again is True  # released


def test_second_concurrent_fetch_is_skipped_with_locked_row(session):
    worker = Worker(make_settings(), get_session_factory(), get_engine())
    called = []
    with advisory_lock(get_engine(), "fetch") as got:
        assert got
        worker._run_locked("fetch", lambda: called.append(1))
    assert not called
    session.expire_all()
    run = session.scalars(select(JobRun)).one()
    assert (run.job, run.status, run.error, run.source_id) == ("fetch", "skipped", "locked", None)
    assert run.counts == {"reason": "locked"}
    worker._run_locked("fetch", lambda: called.append(1))
    assert called == [1]


def test_record_skipped_allows_null_source(session):
    record_skipped(get_session_factory(), "retention", "locked")
    session.expire_all()
    assert session.scalars(select(JobRun)).one().source_id is None


class _Feeds:
    def __init__(self, stop: threading.Event | None = None):
        self.stop = stop
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        if self.stop is not None:
            self.stop.set()  # a stop request arrives while the first source is being fetched
        body = b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title><item><title>A</title>' \
               b'<guid>g1</guid><description>d</description></item></channel></rss>'
        return httpx.Response(200, content=body, headers={"content-type": "application/rss+xml"})


def test_stop_flag_stops_between_sources(session):
    stop = threading.Event()
    feeds = _Feeds(stop)
    settings = make_settings()
    rss = RssAdapter(settings, transport=httpx.MockTransport(feeds), clock=lambda: NOW)
    profile = Profile(ietf=Ietf(), feeds=[Feed(url="https://example.com/a.xml"), Feed(url="https://example.com/b.xml")])
    outs = run_fetch(
        get_session_factory(), settings, adapters={"rss": rss}, profile=profile, now=NOW, stop=stop
    )
    assert len(outs) == 1 and outs[0].status == "ok"  # the running source finished, the next was not started
    assert len(feeds.urls) == 1
    session.expire_all()
    assert len(session.scalars(select(JobRun)).all()) == 1


def test_scheduler_setup_registers_jobs_without_starting(infra):
    settings = make_settings(tz="Asia/Tokyo", fetch_interval_minutes=15)
    worker = Worker(settings, get_session_factory(), get_engine())
    now = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    scheduler = build_scheduler(worker, now=now)
    assert isinstance(scheduler, BlockingScheduler) and not scheduler.running
    jobs = {j.id: j for j in scheduler.get_jobs()}
    assert set(jobs) == {"fetch", "retention", "summarize"}
    fetch, retention = jobs["fetch"], jobs["retention"]
    assert isinstance(fetch.trigger, IntervalTrigger) and fetch.trigger.interval == timedelta(minutes=15)
    assert fetch.max_instances == 1 and fetch.coalesce is True
    assert fetch.next_run_time == now  # immediate run at start
    assert isinstance(retention.trigger, CronTrigger) and retention.max_instances == 1
    fields = {f.name: str(f) for f in retention.trigger.fields}
    assert fields["hour"] == "3" and fields["minute"] == "30"
    assert str(retention.trigger.timezone) == "Asia/Tokyo"
    assert fetch.func == worker.fetch_job and retention.func == worker.retention_job


def test_startup_summary_and_logging_have_no_secrets(caplog):
    settings = make_settings()
    summary = startup_summary(settings)
    caplog.set_level(logging.INFO)
    logging.getLogger("sounding.worker").info("starting worker: %s", summary)
    text = caplog.text + repr(summary) + repr(settings)
    for secret in (CONTACT, SECRET, "db-pass-9d2e", "dbuser", "sk-test-llm-key-5c1b"):
        assert secret not in text
    assert summary["fetch_contact"] == "set" and summary["identity_secret"] == "set"
    unset = startup_summary(make_settings(fetch_contact="", identity_secret="change-me-x"))
    assert unset["fetch_contact"] == "NOT SET" and unset["identity_secret"] == "NOT SET"
