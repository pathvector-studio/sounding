"""run_summarize, the worker's summarize job and the summarize/show commands (real Postgres, FakeProvider)."""

from __future__ import annotations

import logging
import re
import threading
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select

from sounding import jobs
from sounding.cli import format_status, main
from sounding.config import Settings, get_settings
from sounding.db import get_engine, get_session_factory
from sounding.llm import FakeProvider, LLMClient
from sounding.models import ConversationState, JobRun, LlmCall, Source
from sounding.profile import Ietf, Profile
from sounding.worker import Worker, build_scheduler
from test_state import add, echo, good_state

KEY = "sk-test-llm-key-5c1b"
DB_URL = "postgresql+psycopg://dbuser:db-pass-9d2e@localhost:1/none"
ADDR = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def settings(**kw) -> Settings:
    base = dict(
        database_url=DB_URL, llm_api_key=KEY, llm_provider="anthropic", profile_path="/nonexistent/profile.yaml",
        llm_daily_budget_usd="0",
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


PROFILE = Profile(ietf=Ietf(working_groups=["sidrops"]))


@pytest.fixture
def sf(session):
    return get_session_factory()


@pytest.fixture
def src(session):
    s = Source(kind="ietf_mail", key="sidrops")
    session.add(s)
    session.flush()
    return s


def client_for(sf, script, budget="0", **kw):
    fake = FakeProvider(script, **kw)
    return fake, LLMClient(fake, sf, tz="Asia/Tokyo", daily_budget_usd=Decimal(budget), default_max_tokens=500)


def runs(session):
    session.expire_all()
    return list(session.scalars(select(JobRun).where(JobRun.job == "summarize")))


def three(session, src):
    return [add(session, src, f"<{n}@example.com>", subject=f"[sidrops] draft-ietf-sidrops-x topic {n}", hours=n).conversation_id for n in range(3)]


def test_llm_disabled_skips_and_builds_no_provider(session, sf, src, monkeypatch, caplog):
    three(session, src)

    def boom(*a, **k):
        raise AssertionError("a provider must not be built")

    monkeypatch.setattr("sounding.llm.providers.AnthropicProvider", boom)
    monkeypatch.setattr("sounding.llm.providers.OpenAIProvider", boom)
    caplog.set_level(logging.DEBUG)
    rep = jobs.run_summarize(sf, settings(llm_provider="none"), profile=PROFILE)
    assert (rep.status, rep.reason) == ("skipped", "llm_disabled")
    (run,) = runs(session)
    assert (run.status, run.error, run.counts, run.source_id) == ("skipped", "llm_disabled", {"reason": "llm_disabled"}, None)
    assert session.scalars(select(LlmCall)).all() == [] and session.scalars(select(ConversationState)).all() == []


def test_full_run_creates_states_and_one_job_row(session, sf, src, caplog):
    ids = three(session, src)
    fake, client = client_for(sf, echo())
    caplog.set_level(logging.DEBUG)
    rep = jobs.run_summarize(sf, settings(), client=client, profile=PROFILE)
    assert rep.status == "ok" and fake.calls == 3
    (run,) = runs(session)
    c = run.counts
    assert run.status == "ok" and run.job == "summarize" and run.source_id is None
    assert (c["selected"], c["created"], c["updated"], c["no_new"], c["failed"], c["llm_calls"]) == (3, 3, 0, 0, {}, 3)
    assert Decimal(c["cost_usd"]) == session.scalar(select(func.sum(LlmCall.cost_usd))) > 0
    assert c["budget_skipped"] == 0 and "budget_skipped_ids" not in c
    assert {s.conversation_id for s in session.scalars(select(ConversationState))} == set(ids)
    # logs: ids and statuses only
    assert "summarize conversation" in caplog.text
    for banned in (KEY, "topic 1", "I support this", "Alice Example", "example.com"):
        assert banned not in caplog.text
    # second run: nothing selected
    rep2 = jobs.run_summarize(sf, settings(), client=client, profile=PROFILE)
    assert rep2.counts["selected"] == 0 and fake.calls == 3


def test_budget_exceeded_stops_calls_and_records_ids(session, sf, src):
    ids = three(session, src)
    # each call costs ~ (1000 in x $4 + 200 out x $20)/1M = $0.008; the budget allows exactly one
    fake, client = client_for(sf, echo(), budget="0.005")
    rep = jobs.run_summarize(sf, settings(llm_daily_budget_usd="0.005"), client=client, profile=PROFILE)
    assert fake.calls == 1 and rep.status == "ok"
    (run,) = runs(session)
    c = run.counts
    assert c["created"] == 1 and c["budget_skipped"] == 2 and len(c["budget_skipped_ids"]) == 2
    assert set(c["budget_skipped_ids"]) | {o.conversation_id for o in rep.outcomes if o.status == "created"} == set(ids)
    assert "budget" in c["budget_skipped_note"]


def test_config_error_stops_run_and_fails(session, sf, src):
    three(session, src)
    fake, client = client_for(sf, echo(), models={"cheap": None, "main": None})
    rep = jobs.run_summarize(sf, settings(), client=client, profile=PROFILE)
    assert (rep.status, rep.reason) == ("failed", "config_error")
    (run,) = runs(session)
    assert run.status == "failed" and run.error == "config_error"
    assert run.counts["failed"] == {"config_error": 1} and run.counts["stopped"] == 2 and fake.calls == 0


def test_other_failures_are_counted_and_run_continues(session, sf, src):
    three(session, src)
    bad = {"nonsense": True}
    fake, client = client_for(sf, [bad, bad, echo(), echo(), echo()])
    rep = jobs.run_summarize(sf, settings(), client=client, profile=PROFILE)
    (run,) = runs(session)
    assert run.status == "ok" and run.counts["created"] == 2 and sum(run.counts["failed"].values()) == 1
    assert rep.status == "ok"
    # everything failing -> failed
    session.expire_all()
    fake2, client2 = client_for(sf, bad)
    add(session, src, "<z@example.com>", subject="[sidrops] draft-ietf-sidrops-x z", hours=9)
    rep2 = jobs.run_summarize(sf, settings(), client=client2, profile=PROFILE, only_conversation=_last_cid(session))
    assert (rep2.status, rep2.reason) == ("failed", "all_failed")


def _last_cid(session):
    from sounding.models import Conversation

    return session.scalars(select(Conversation.id).order_by(Conversation.id.desc())).first()


def test_stop_between_conversations(session, sf, src):
    three(session, src)
    stop = threading.Event()
    fake, client = client_for(sf, lambda req: (stop.set(), echo()(req))[1])
    rep = jobs.run_summarize(sf, settings(), client=client, profile=PROFILE, stop=stop)
    assert fake.calls == 1 and rep.counts["created"] == 1 and rep.counts["stopped"] == 2
    (run,) = runs(session)
    assert run.status == "ok" and run.counts["stopped"] == 2


def test_only_conversation(session, sf, src):
    a, b, _ = three(session, src)
    fake, client = client_for(sf, echo())
    rep = jobs.run_summarize(sf, settings(), client=client, profile=PROFILE, only_conversation=b)
    assert rep.counts["selected"] == 1 and fake.calls == 1
    assert [s.conversation_id for s in session.scalars(select(ConversationState))] == [b]


def test_scheduler_has_summarize_at_six(infra):
    worker = Worker(settings(tz="Asia/Tokyo"), get_session_factory(), get_engine())
    job = {j.id: j for j in build_scheduler(worker, now=datetime(2026, 11, 8, 12, tzinfo=UTC)).get_jobs()}["summarize"]
    assert isinstance(job.trigger, CronTrigger) and job.max_instances == 1 and job.coalesce is True
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "6" and fields["minute"] == "0" and str(job.trigger.timezone) == "Asia/Tokyo"
    assert job.func == worker.summarize_job


def test_worker_summarize_job_locks_and_skips(session, sf, monkeypatch):
    worker = Worker(settings(llm_provider="none"), sf, get_engine())
    worker.summarize_job()
    assert [r.status for r in runs(session)] == ["skipped"]
    with jobs.advisory_lock(get_engine(), "summarize") as got:
        assert got
        worker.summarize_job()
    assert runs(session)[-1].error == "locked"
    worker.stop.set()
    before = len(runs(session))
    worker.summarize_job()
    assert len(runs(session)) == before


def test_cli_summarize_show_and_status(session, sf, src, tmp_path, monkeypatch, capsys):
    from sounding import cli  # noqa: F401

    profile = tmp_path / "profile.yaml"
    profile.write_text("ietf:\n  working_groups: [sidrops]\n")
    monkeypatch.setenv("PROFILE_PATH", str(profile))
    monkeypatch.setenv("LLM_PROVIDER", "none")
    get_settings.cache_clear()
    try:
        ids = three(session, src)
        assert main(["summarize", "--once"]) == 0
        assert "skipped" in capsys.readouterr().out
        assert main(["summarize"]) == 2
        capsys.readouterr()

        fake, client = client_for(sf, echo())
        monkeypatch.setattr(jobs, "build_client", lambda s, f: client)
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        get_settings.cache_clear()
        assert main(["summarize", "--once", "--conversation", str(ids[0])]) == 0
        out = capsys.readouterr().out
        assert f"conversation {ids[0]}: created v1" in out and "summarize: ok selected=1 created=1" in out
        assert main(["summarize", "--once"]) == 0
        assert "selected=2 created=2" in capsys.readouterr().out

        assert main(["show", str(ids[0])]) == 0
        shown = capsys.readouterr().out
        assert "WGLC in progress" in shown and "AS_SET handling" in shown
        assert main(["show", "99999"]) == 0
        assert "no state for conversation 99999" in capsys.readouterr().out
        assert main(["show", str(ids[0]), "--language", "ja"]) == 0
        assert "no state" in capsys.readouterr().out

        get_settings.cache_clear()
        out = format_status(session, settings())
        assert "last summarize:" in out and "llm today" in out
        assert KEY not in out
        # config_error -> exit code 1
        _, bad = client_for(sf, echo(), models={"cheap": None, "main": None})
        monkeypatch.setattr(jobs, "build_client", lambda s, f: bad)
        add(session, src, "<n@example.com>", subject="[sidrops] draft-ietf-sidrops-x n", hours=20)
        assert main(["summarize", "--once"]) == 1
    finally:
        get_settings.cache_clear()
