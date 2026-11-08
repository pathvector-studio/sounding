"""`python -m sounding <command>` (argparse; no extra dependency)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sounding.config import Settings
from sounding.models import Conversation, Event, Item, JobRun, Source

BACKEND_DIR = Path(__file__).resolve().parent.parent
RECENT_JOB_RUNS = 10


def alembic_config():
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


def run_migrations() -> None:
    from alembic import command

    command.upgrade(alembic_config(), "head")


def cmd_migrate(_args: argparse.Namespace) -> int:
    run_migrations()
    print("migrated to head")
    return 0


def _counts(session: Session, model: type, column) -> dict[int, int]:
    rows = session.execute(select(column, func.count()).select_from(model).group_by(column)).all()
    return {row[0]: row[1] for row in rows}


def format_llm_status(session: Session, settings: Settings) -> str:
    from sounding.llm import calls_today, spend_today

    budget = settings.llm_daily_budget_usd
    limit = f"budget ${budget:.2f}" if budget > 0 else "budget unlimited"
    counts = calls_today(session, settings.tz)
    done = " ".join(f"{k}={counts.get(k, 0)}" for k in ("ok", "failed", "reused"))
    return f"llm today ({settings.tz}): ${spend_today(session, settings.tz):.4f} spent, {limit}, provider={settings.llm_provider}, calls {done}"


def format_status(session: Session, settings: Settings | None = None) -> str:
    from sounding.config import get_settings

    settings = settings or get_settings()
    lines = ["recent job_runs:"]
    runs = session.scalars(select(JobRun).order_by(JobRun.started_at.desc(), JobRun.id.desc()).limit(RECENT_JOB_RUNS)).all()
    if not runs:
        lines.append("  (none)")
    for run in runs:
        source = f" source={run.source_id}" if run.source_id is not None else ""
        counts = f" counts={run.counts}" if run.counts else ""
        error = f" error={run.error}" if run.error else ""
        lines.append(f"  {run.started_at.isoformat()} {run.job}{source} {run.status}{counts}{error}")

    lines.append("sources:")
    sources = session.scalars(select(Source).order_by(Source.kind, Source.key)).all()
    if not sources:
        lines.append("  (none)")
    items = _counts(session, Item, Item.source_id)
    conversations = _counts(session, Conversation, Conversation.source_id)
    events = _counts(session, Event, Event.source_id)
    for s in sources:
        lines.append(
            f"  {s.kind} {s.key}: items={items.get(s.id, 0)} conversations={conversations.get(s.id, 0)} "
            f"events={events.get(s.id, 0)}{'' if s.enabled else ' (disabled)'}"
        )
    lines.append(format_llm_status(session, settings))
    last = session.scalar(select(JobRun).where(JobRun.job == "summarize").order_by(JobRun.started_at.desc(), JobRun.id.desc()).limit(1))
    if last is None:
        lines.append("last summarize: (never)")
    else:
        reason = f" error={last.error}" if last.error else ""
        lines.append(f"last summarize: {last.started_at.isoformat()} {last.status}{reason} counts={last.counts}")
    return "\n".join(lines)


def cmd_status(_args: argparse.Namespace) -> int:
    from sounding.db import session_scope

    with session_scope() as session:
        print(format_status(session))
    return 0


def format_sources(session: Session) -> str:
    sources = session.scalars(select(Source).order_by(Source.kind, Source.key)).all()
    if not sources:
        return "(no sources; add some to the profile)"
    lines = []
    for s in sources:
        fetched = s.last_fetched_at.isoformat() if s.last_fetched_at else "never"
        lines.append(
            f"{s.kind} {s.key} enabled={'yes' if s.enabled else 'no'} last_fetched={fetched} "
            f"status={s.last_status or '-'} consecutive_failures={s.consecutive_failures or 0}"
        )
    return "\n".join(lines)


def cmd_sources(_args: argparse.Namespace) -> int:
    from sounding.db import session_scope

    with session_scope() as session:
        print(format_sources(session))
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    from sounding.config import get_settings
    from sounding.db import get_session_factory
    from sounding.jobs import run_fetch
    from sounding.profile import ProfileError

    if not args.once:
        print("only --once is supported here; the scheduled fetch is run by the worker", file=sys.stderr)
        return 2
    try:
        outcomes = run_fetch(get_session_factory(), get_settings(), only_source=args.source)
    except ProfileError as exc:
        print(f"profile error: {exc}", file=sys.stderr)
        return 1
    if not outcomes:
        print("nothing to fetch (no matching enabled source with an adapter)")
    for o in outcomes:
        detail = f" error={o.error}" if o.error else ""
        print(f"{o.kind} {o.key}: {o.status} {o.counts}{detail}")
    return 1 if any(o.status == "failed" for o in outcomes) else 0


def cmd_retention(args: argparse.Namespace) -> int:
    from sounding.config import get_settings
    from sounding.db import get_session_factory
    from sounding.jobs import run_retention

    if not args.once:
        print("only --once is supported here; the scheduled retention is run by the worker", file=sys.stderr)
        return 2
    counts = run_retention(get_session_factory(), get_settings())
    print(f"retention: {counts}")
    return 0


def cmd_summarize(args: argparse.Namespace) -> int:
    from sounding.config import get_settings
    from sounding.db import get_session_factory
    from sounding.jobs import run_summarize
    from sounding.profile import ProfileError

    if not args.once:
        print("only --once is supported here; the scheduled summarize is run by the worker", file=sys.stderr)
        return 2
    try:
        report = run_summarize(get_session_factory(), get_settings(), only_conversation=args.conversation)
    except ProfileError as exc:
        print(f"profile error: {exc}", file=sys.stderr)
        return 1
    if report.status == "skipped":
        print("summarize skipped: LLM_PROVIDER is none (nothing was sent)")
        return 0
    for o in report.outcomes:
        extra = f" {o.code}" if o.code else (f" v{o.version}" if o.version else "")
        print(f"conversation {o.conversation_id}: {o.status}{extra}")
    for cid in report.budget_skipped_ids:
        print(f"conversation {cid}: not summarized (daily budget exceeded)")
    c = report.counts
    print(
        f"summarize: {report.status}{' (' + report.reason + ')' if report.reason else ''} selected={c['selected']} "
        f"created={c['created']} updated={c['updated']} rebuilt={c['rebuilt']} no_new={c['no_new']} "
        f"failed={c['failed']} budget_skipped={c['budget_skipped']} llm_calls={c['llm_calls']} cost=${c['cost_usd']}"
    )
    return 1 if report.reason == "config_error" else 0


def cmd_show(args: argparse.Namespace) -> int:
    from sounding.config import get_settings
    from sounding.db import session_scope
    from sounding.models import ConversationState
    from sounding.profile import ProfileError, load_profile
    from sounding.state import render_state

    language = args.language
    if not language:
        try:
            language = load_profile(get_settings().profile_path).profile.language or "en"
        except ProfileError:
            language = "en"
    with session_scope() as session:
        row = session.scalar(
            select(ConversationState)
            .where(ConversationState.conversation_id == args.conversation_id, ConversationState.language == language)
            .order_by(ConversationState.version.desc())
            .limit(1)
        )
        if row is None:
            print(f"no state for conversation {args.conversation_id} in language {language} (run: summarize --once)")
            return 0
        print(render_state(row))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m sounding")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="alembic upgrade head").set_defaults(func=cmd_migrate)
    sub.add_parser("status", help="recent job_runs and per-source counts").set_defaults(func=cmd_status)
    sub.add_parser("sources", help="sources and their state").set_defaults(func=cmd_sources)
    fetch = sub.add_parser("fetch", help="fetch sources")
    fetch.add_argument("--once", action="store_true", help="fetch once and exit")
    fetch.add_argument("--source", metavar="KEY", help="only this source (list name or feed URL)")
    fetch.set_defaults(func=cmd_fetch)
    retention = sub.add_parser("retention", help="clear old item bodies (CONTENT_RETENTION_DAYS)")
    retention.add_argument("--once", action="store_true", help="run once and exit")
    retention.set_defaults(func=cmd_retention)
    summarize = sub.add_parser("summarize", help="generate 'where it stands' for conversations with new messages")
    summarize.add_argument("--once", action="store_true", help="run once and exit")
    summarize.add_argument("--conversation", type=int, metavar="ID", help="only this conversation id")
    summarize.set_defaults(func=cmd_summarize)
    show = sub.add_parser("show", help="print the latest stored state of a conversation")
    show.add_argument("conversation_id", type=int)
    show.add_argument("--language", metavar="xx", help="state language (default: profile.language)")
    show.set_defaults(func=cmd_show)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
