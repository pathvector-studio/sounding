"""The worker: ``python -m sounding.worker``.

Runs migrations, then a blocking APScheduler loop (``fetch`` every FETCH_INTERVAL_MINUTES and once at
start, ``retention`` daily at 03:30, ``summarize`` daily at 06:00 and only when LLM_PROVIDER is set). Each run takes a PostgreSQL advisory lock so that a second worker
never runs the same job at the same time. SIGTERM / SIGINT stop scheduling and let a running fetch
finish its current source.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from sounding.config import Settings
from sounding.jobs import SessionFactory, advisory_lock, record_skipped, run_fetch, run_retention, run_summarize
from sounding.profile import ProfileError

log = logging.getLogger("sounding.worker")

RETENTION_HOUR, RETENTION_MINUTE = 3, 30
SUMMARIZE_HOUR, SUMMARIZE_MINUTE = 6, 0


def startup_summary(settings: Settings) -> dict[str, object]:
    """Non-secret view of the configuration (secrets appear only as set / not set)."""
    return {
        "tz": settings.tz,
        "fetch_interval_minutes": settings.fetch_interval_minutes,
        "content_retention_days": settings.content_retention_days,
        "ietf_mail_fetch": settings.ietf_mail_fetch,
        "ietf_imap_host": settings.ietf_imap_host,
        "profile_path": settings.profile_path,
        "fetch_contact": "set" if settings.fetch_contact_is_set else "NOT SET",
        "identity_secret": "set" if settings.identity_secret_is_set else "NOT SET",
    }


class Worker:
    def __init__(self, settings: Settings, session_factory: SessionFactory, engine) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.engine = engine
        self.stop = threading.Event()
        self._active = 0
        self._cond = threading.Condition()

    def _run_locked(self, job: str, fn: Callable[[], object]) -> None:
        if self.stop.is_set():
            return
        with self._cond:
            self._active += 1
        try:
            with advisory_lock(self.engine, job) as got:
                if not got:
                    log.info("job %s skipped: another worker holds the lock", job)
                    record_skipped(self.session_factory, job, "locked")
                    return
                fn()
        except ProfileError as exc:
            log.error("job %s: profile error: %s", job, exc)
        except Exception:  # noqa: BLE001 - one bad run must not kill the scheduler
            log.exception("job %s failed", job)
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify_all()

    def fetch_job(self) -> None:
        def go() -> None:
            outcomes = run_fetch(self.session_factory, self.settings, stop=self.stop)
            summary = ", ".join(f"{o.key}={o.status}" for o in outcomes) or "nothing to fetch"
            log.info("fetch finished: %s", summary)

        self._run_locked("fetch", go)

    def retention_job(self) -> None:
        def go() -> None:
            log.info("retention finished: %s", run_retention(self.session_factory, self.settings))

        self._run_locked("retention", go)

    def summarize_job(self) -> None:
        def go() -> None:
            report = run_summarize(self.session_factory, self.settings, stop=self.stop)
            log.info("summarize finished: %s %s", report.status, report.reason or "")

        self._run_locked("summarize", go)

    def wait_idle(self) -> None:
        with self._cond:
            self._cond.wait_for(lambda: self._active == 0)


def build_scheduler(
    worker: Worker, *, now: datetime | None = None, scheduler_cls: type[BlockingScheduler] = BlockingScheduler
) -> BlockingScheduler:
    """Register the jobs (does not start the scheduler)."""
    tz = ZoneInfo(worker.settings.tz)
    now = now or datetime.now(tz)
    scheduler = scheduler_cls(timezone=tz)
    scheduler.add_job(
        worker.fetch_job,
        IntervalTrigger(minutes=max(1, worker.settings.fetch_interval_minutes), timezone=tz),
        id="fetch", name="fetch", max_instances=1, coalesce=True, next_run_time=now,
    )
    scheduler.add_job(
        worker.retention_job,
        CronTrigger(hour=RETENTION_HOUR, minute=RETENTION_MINUTE, timezone=tz),
        id="retention", name="retention", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        worker.summarize_job,
        CronTrigger(hour=SUMMARIZE_HOUR, minute=SUMMARIZE_MINUTE, timezone=tz),
        id="summarize", name="summarize", max_instances=1, coalesce=True,
    )
    return scheduler


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from sounding.cli import run_migrations
    from sounding.config import get_settings
    from sounding.db import dispose_engine, get_engine, get_session_factory

    settings = get_settings()
    log.info("starting worker: %s", startup_summary(settings))
    if not settings.fetch_contact_is_set:
        log.warning("FETCH_CONTACT is not set: IETF lists will be skipped")
    if not settings.identity_secret_is_set:
        log.warning("IDENTITY_SECRET is not set: IETF lists will be skipped")
    run_migrations()

    worker = Worker(settings, get_session_factory(), get_engine())
    scheduler = build_scheduler(worker)

    def on_signal(signum: int, _frame) -> None:
        log.info("signal %d received: stopping after the current source", signum)
        worker.stop.set()
        try:
            scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001 - already stopped (second signal) or not yet started
            pass

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        scheduler.start()
    finally:
        worker.stop.set()
        worker.wait_idle()
        dispose_engine()
    log.info("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
