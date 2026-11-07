from datetime import UTC, datetime

from sounding.cli import format_status, main
from sounding.models import Item, JobRun, Source


def test_status_empty(session):
    out = format_status(session)
    assert "(none)" in out


def test_status_counts(session):
    s = Source(kind="rss", key="https://example.com/feed.xml")
    session.add(s)
    session.flush()
    now = datetime(2026, 11, 8, tzinfo=UTC)
    session.add(Item(source_id=s.id, external_id="1", title="t", published_at=now))
    session.add(JobRun(job="fetch", source_id=s.id, started_at=now, status="ok", counts={"new": 1}))
    session.flush()
    out = format_status(session)
    assert "rss https://example.com/feed.xml: items=1 conversations=0 events=0" in out
    assert "fetch source=" in out and "ok" in out


def test_migrate_and_status_commands(infra, capsys):
    assert main(["migrate"]) == 0
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "migrated to head" in out and "recent job_runs:" in out
