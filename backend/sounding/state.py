"""Generate and update a conversation's "where it stands" (spec phase 2, R4-R6, R9).

Source independent: the only source-specific text is the prompt fragment chosen by ``FRAGMENTS``
(``sounding/prompts/``). Entry points for the worker/CLI (item 4):

* ``conversations_to_summarize(session, profile, *, language, since=None)`` - ids to process;
* ``summarize_conversation(session_factory, client, conversation_id, *, language, now=None)`` -> ``Outcome``;
* ``render_state(row)`` - plain text for ``show``.

Flow of ``summarize_conversation``
1. Load the latest stored state for (conversation, language). Human messages not in its
   ``covered_item_ids`` are "new". None new -> ``no_new``, no LLM call.
2. A new message published before the state's ``last_item_published_at`` (a completed ancestor, a
   merged conversation) -> ``rebuilt``: everything again, ignoring the previous state.
3. Otherwise ``updated``: previous state JSON (without ``sources``/``process_state``) + only the new
   messages; the model returns the whole next state and ``changed_since_last``.
4. No previous state -> ``created`` (first version, ``changed_since_last`` null; also null on a rebuild).
5. Long input (estimated tokens > ``MAX_INPUT_TOKENS``) is split in chronological chunks. Chunk 1 is
   an initial call on the ``cheap`` tier, middle chunks are ``continue`` calls on ``cheap`` carrying the
   intermediate state, the last chunk is a ``continue`` call on ``main`` that yields the final state.
   Intermediate states are validated like final ones and never stored.
6. The model's output is post-validated (``validate_state``) and stored as a new
   ``conversation_states`` row, with ``sources`` (ref -> url/date/author) built from the DB.

Model refs are ``m<items.id>`` (stable, no address in them). LLMError -> ``Outcome.status == "failed"``
with the code and nothing stored; only programming errors propagate.

``STATE_PROMPT_VERSION``: bump when ``SYSTEM_GENERIC``, a fragment, ``STATE_SCHEMA`` or the user
prompt layout changes in a way that changes outputs (it is part of the llm_calls reuse hash, so a
bump makes old cached results unusable on purpose). Stored states keep the version they were made with.
"""

from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sounding.llm import LLMClient, LLMError
from sounding.models import Conversation, ConversationState, Event, Item, Source
from sounding.preprocess import preprocess_body
from sounding.profile import Profile
from sounding.prompts.common import SYSTEM_GENERIC
from sounding.prompts.ietf import IETF_FRAGMENT
from sounding.text import mask_addresses

log = logging.getLogger(__name__)

STATE_PROMPT_VERSION = "state-v1"
PURPOSE = "conversation_state"
PURPOSE_CHUNK = "conversation_state_chunk"

MAX_INPUT_TOKENS = 60_000  # estimated (chars / 4) message tokens per call; above this the input is chunked
MAX_MESSAGE_CHARS = 20_000  # one preprocessed body is cut here (pasted logs, configs); a note is added

FRAGMENTS: dict[str, str] = {"ietf_mail": IETF_FRAGMENT}


def system_prompt(source_kind: str) -> str:
    """Fixed per source kind: nothing per conversation or per language is ever in here."""
    fragment = FRAGMENTS.get(source_kind)
    return SYSTEM_GENERIC if not fragment else f"{SYSTEM_GENERIC}\n{fragment}"


def _arr(item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": item}


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_STR = {"type": "string"}
_STRS = _arr(_STR)

STATE_SCHEMA: dict[str, Any] = _obj(
    {
        "status_line": _STR,
        "issues": _arr(
            _obj(
                {
                    "issue": _STR,
                    "positions": _arr(_obj({"stance": _STR, "by": _STRS, "refs": _STRS})),
                    "status": {"type": "string", "enum": ["agreed", "leaning", "open", "parked"]},
                    "evidence": {"type": ["string", "null"]},
                    "refs": _STRS,
                }
            )
        ),
        "agreed": _arr(_obj({"text": _STR, "refs": _STRS})),
        "open_questions": _STRS,
        "next": _STRS,
        "changed_since_last": {"type": ["string", "null"]},
        "sources_used": _STRS,
    }
)


@dataclass
class Outcome:
    conversation_id: int
    # created | updated | rebuilt | no_new | skipped | failed
    status: str
    code: str | None = None  # LLMError code when failed; "merged" / "not_found" when skipped
    version: int | None = None
    downgraded: int = 0  # unsupported agreed/leaning issues set to open + unsupported agreed items moved
    llm_calls: int = 0
    llm_call_ids: list[int] = field(default_factory=list)

    @property
    def stored(self) -> bool:
        return self.status in ("created", "updated", "rebuilt")


# --- selection ---------------------------------------------------------------------------------

def _term_re(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(term.strip())}(?!\w)", re.IGNORECASE)


def tracked(profile: Profile, text: str) -> bool:
    """Does ``text`` mention a tracked draft prefix or a ``keywords.include`` term, and no ``keywords.exclude`` term?"""
    low = text.lower()
    if any(t.strip() and _term_re(t).search(text) for t in profile.keywords.exclude):
        return False
    if any(p.strip() and p.strip().lower() in low for p in profile.ietf.drafts):
        return True
    return any(t.strip() and _term_re(t).search(text) for t in profile.keywords.include)


def conversations_to_summarize(
    session: Session, profile: Profile, *, language: str, since: datetime | None = None
) -> list[int]:
    """Final (not merged) conversations of enabled ietf_mail sources with a human message the latest
    state for ``language`` does not cover; most recent activity first.

    Lists in ``ietf.extra_lists`` (and not in ``working_groups``) only count when their subject or a
    human body mentions a tracked draft or ``keywords.include`` term (and no ``keywords.exclude`` term).
    ``since`` limits to conversations with activity at or after it.
    """
    base = (
        select(Conversation.id, Conversation.subject, Source.key)
        .join(Source, Source.id == Conversation.source_id)
        .where(Source.kind == "ietf_mail", Source.enabled.is_(True), Conversation.merged_into.is_(None))
    )
    if since is not None:
        base = base.where(Conversation.last_message_at >= since)
    convs = session.execute(base.order_by(Conversation.last_message_at.desc(), Conversation.id.desc())).all()
    if not convs:
        return []
    ids = [c.id for c in convs]

    human: dict[int, list[int]] = {}
    for cid, iid in session.execute(
        select(Item.conversation_id, Item.id).where(Item.conversation_id.in_(ids), Item.is_auto.is_(False))
    ):
        human.setdefault(cid, []).append(iid)
    covered: dict[int, set[int]] = {}
    for cid, cov in session.execute(
        select(ConversationState.conversation_id, ConversationState.covered_item_ids)
        .where(ConversationState.conversation_id.in_(ids), ConversationState.language == language)
        .order_by(ConversationState.conversation_id, ConversationState.version)
    ):
        covered[cid] = set(cov)  # ascending versions: the last one wins

    wg = set(profile.ietf.working_groups)
    extra = set(profile.ietf.extra_lists) - wg
    out: list[int] = []
    for c in convs:
        if not any(i not in covered.get(c.id, set()) for i in human.get(c.id, [])):
            continue
        if c.key in extra:
            bodies = session.scalars(
                select(Item.body_text).where(Item.conversation_id == c.id, Item.is_auto.is_(False))
            ).all()
            if not tracked(profile, "\n".join([c.subject or "", *[b for b in bodies if b]])):
                continue
        out.append(c.id)
    return out


# --- input building ----------------------------------------------------------------------------

_DELETED = {
    "ja": "[本文は保持期間を過ぎて削除済み]",
    "en": "[body deleted after the retention period]",
}
_TRUNCATED = "\n[... message cut: too long ...]"
_DELIM = re.compile(r"<(?=/?\s*(?:message|messages|new_messages|previous_state|events|event|conversation)\b)", re.I)


def _neutralise(text: str) -> str:
    """Make text that looks like one of our delimiters inert (the model sees ``&lt;message``)."""
    return _DELIM.sub("&lt;", text)


def _attr(text: str) -> str:
    return html.escape(" ".join(mask_addresses(text).split()), quote=True)


def ref_for(item_id: int) -> str:
    return f"m{item_id}"


def _tokens(chars: int) -> int:
    return chars // 4 + 1


def render_message(item: Item, language: str) -> str:
    if item.body_text is None:
        body = _DELETED.get(language.split("-")[0].lower(), _DELETED["en"])
    else:
        body = mask_addresses(preprocess_body(item.body_text).text)
        if len(body) > MAX_MESSAGE_CHARS:
            body = body[:MAX_MESSAGE_CHARS] + _TRUNCATED
    date = item.published_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")
    author = _attr(item.author_name or "(unknown)")
    return f'<message ref="{ref_for(item.id)}" date="{date}" author="{author}">\n{_neutralise(body)}\n</message>'


def render_events(events: list[Event]) -> str:
    if not events:
        return ""
    lines = [
        f"- {e.occurred_at.astimezone(UTC).strftime('%Y-%m-%d')} {_attr(e.kind)} {_attr(e.ref or '-')}" for e in events
    ]
    return "<events>\n" + "\n".join(lines) + "\n</events>\n"


def _safe_json(data: Any) -> str:
    """JSON that cannot contain a closing delimiter ("</" becomes "<\\/", still valid JSON)."""
    return json.dumps(data, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")


def build_user_prompt(
    *, mode: str, language: str, subject: str, messages: list[str], events: list[Event],
    previous: dict[str, Any] | None,
) -> str:
    task = {
        "initial": "Task: initial. Build the first version of the state from the messages below.",
        "update": "Task: update. Output the complete next state from the previous state and the new messages.",
        "continue": "Task: continue. The conversation is given in chronological parts; output the complete state so far.",
    }[mode]
    parts = [f"{task}\nOutput language: {language}\n", f'<conversation subject="{_attr(subject)}">\n']
    ev = render_events(events)
    if ev:
        parts.append(ev)
    if previous is not None:
        parts.append(f"<previous_state>\n{_safe_json(previous)}\n</previous_state>\n")
    tag = "messages" if previous is None else "new_messages"
    parts.append(f"<{tag}>\n" + "\n".join(messages) + f"\n</{tag}>\n</conversation>\n")
    return "".join(parts)


def _chunks(texts: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    """Greedy chronological split so each chunk's estimated tokens stay within MAX_INPUT_TOKENS."""
    out: list[list[tuple[int, str]]] = [[]]
    size = 0
    for item_id, text in texts:
        t = _tokens(len(text))
        if out[-1] and size + t > MAX_INPUT_TOKENS:
            out.append([])
            size = 0
        out[-1].append((item_id, text))
        size += t
    return out


# --- post-validation (R4) ----------------------------------------------------------------------

def _keep(refs: list[str], valid: set[str]) -> list[str]:
    return list(dict.fromkeys(r for r in refs if r in valid))


def validate_state(data: dict[str, Any], valid_refs: set[str]) -> tuple[dict[str, Any], int]:
    """Drop unknown refs; return (clean state, number of downgrades).

    * agreed/leaning issue with no valid ref (issue.refs + positions' refs) -> status ``open``.
    * ``agreed`` entry with no valid ref -> moved to ``open_questions`` with its text. Dropping it would
      lose what the model believed; as an open question it is shown honestly as unconfirmed.
    """
    out = json.loads(json.dumps(data))  # deep copy
    downgraded = 0
    for issue in out["issues"]:
        issue["refs"] = _keep(issue["refs"], valid_refs)
        for pos in issue["positions"]:
            pos["refs"] = _keep(pos["refs"], valid_refs)
        supported = bool(issue["refs"]) or any(p["refs"] for p in issue["positions"])
        if issue["status"] in ("agreed", "leaning") and not supported:
            issue["status"] = "open"
            downgraded += 1
    agreed = []
    for a in out["agreed"]:
        a["refs"] = _keep(a["refs"], valid_refs)
        if a["refs"]:
            agreed.append(a)
        else:
            out["open_questions"].append(a["text"])
            downgraded += 1
    out["agreed"] = agreed
    out["sources_used"] = _keep(out["sources_used"], valid_refs)
    return out, downgraded


def _all_refs(state: dict[str, Any]) -> list[str]:
    refs: list[str] = list(state["sources_used"])
    for issue in state["issues"]:
        refs += issue["refs"]
        for p in issue["positions"]:
            refs += p["refs"]
    for a in state["agreed"]:
        refs += a["refs"]
    return list(dict.fromkeys(refs))


# --- generation --------------------------------------------------------------------------------

def _previous_for_prompt(state_json: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in state_json.items() if k not in ("sources", "process_state")}


def _call(
    client: LLMClient, *, purpose: str, tier: str, system: str, user: str, language: str,
    conversation_id: int, valid: set[str], acc: Outcome,
) -> dict[str, Any]:
    result = client.complete(
        purpose=purpose, tier=tier, system=system, user=user, schema=STATE_SCHEMA,  # type: ignore[arg-type]
        prompt_version=STATE_PROMPT_VERSION, language=language, conversation_id=conversation_id,
    )
    acc.llm_calls += 1
    if result.call_id is not None:
        acc.llm_call_ids.append(result.call_id)
    clean, down = validate_state(result.data, valid)
    acc.downgraded += down
    clean["_model"] = result.model  # popped by the caller
    clean["_call_id"] = result.call_id
    return clean


def summarize_conversation(
    session_factory: Callable[[], Session],
    client: LLMClient,
    conversation_id: int,
    *,
    language: str,
    now: datetime | None = None,
) -> Outcome:
    """Create the next state version of one conversation, or say why not. See the module docstring."""
    out = Outcome(conversation_id, "failed")
    with session_factory() as s:
        conv = s.get(Conversation, conversation_id)
        if conv is None:
            return Outcome(conversation_id, "skipped", code="not_found")
        if conv.merged_into is not None:
            return Outcome(conversation_id, "skipped", code="merged")
        source = s.get(Source, conv.source_id)
        system = system_prompt(source.kind if source else "")
        subject = conv.subject or ""
        items = list(
            s.scalars(
                select(Item).where(Item.conversation_id == conversation_id).order_by(Item.published_at, Item.id)
            )
        )
        humans = [i for i in items if not i.is_auto]
        prev = s.scalar(
            select(ConversationState)
            .where(ConversationState.conversation_id == conversation_id, ConversationState.language == language)
            .order_by(ConversationState.version.desc())
            .limit(1)
        )
        covered = set(prev.covered_item_ids) if prev else set()
        new = [i for i in humans if i.id not in covered]
        if not new:
            return Outcome(conversation_id, "no_new")

        rebuild = prev is not None and any(i.published_at < prev.last_item_published_at for i in new)
        base_version = prev.version if prev else 0
        previous_state = _previous_for_prompt(prev.state_json) if prev is not None and not rebuild else None
        send = new if previous_state is not None else humans
        all_events = list(
            s.scalars(select(Event).where(Event.conversation_id == conversation_id).order_by(Event.occurred_at, Event.id))
        )
        if previous_state is not None:  # diff: only facts newer than what the state already saw
            all_events = [e for e in all_events if e.occurred_at > prev.last_item_published_at]  # type: ignore[union-attr]
        rendered = [(i.id, render_message(i, language)) for i in send]
        by_id = {i.id: i for i in items}
        covered_after = sorted({i.id for i in humans})
        last_published = max(i.published_at for i in humans)
        if previous_state is not None:
            status = "updated"
        else:
            status = "rebuilt" if prev is not None else "created"
        s.expunge_all()

    valid = {ref_for(i) for i in covered_after}
    try:
        total = sum(_tokens(len(t)) for _, t in rendered)
        if total <= MAX_INPUT_TOKENS:
            user = build_user_prompt(
                mode="update" if previous_state is not None else "initial", language=language, subject=subject,
                messages=[t for _, t in rendered], events=all_events, previous=previous_state,
            )
            final = _call(
                client, purpose=PURPOSE, tier="main", system=system, user=user, language=language,
                conversation_id=conversation_id, valid=valid, acc=out,
            )
        else:
            parts = _chunks(rendered)
            state = previous_state
            for n, part in enumerate(parts):
                last = n == len(parts) - 1
                mode = "initial" if state is None else ("update" if n == 0 else "continue")
                user = build_user_prompt(
                    mode=mode, language=language, subject=subject, messages=[t for _, t in part],
                    events=all_events if n == 0 else [], previous=state,
                )
                got = _call(
                    client, purpose=PURPOSE if last else PURPOSE_CHUNK, tier="main" if last else "cheap",
                    system=system, user=user, language=language, conversation_id=conversation_id,
                    valid=valid, acc=out,
                )
                if last:
                    final = got
                else:
                    got.pop("_model")
                    got.pop("_call_id")
                    state = got
    except LLMError as exc:
        out.status = "failed"
        out.code = exc.code
        return out

    model = final.pop("_model")
    call_id = final.pop("_call_id")
    if status != "updated":
        final["changed_since_last"] = None
    final["process_state"] = None
    final["sources"] = _build_sources(by_id, _all_refs(final))

    with session_factory() as s:
        try:
            latest = s.scalar(
                select(ConversationState.version)
                .where(ConversationState.conversation_id == conversation_id, ConversationState.language == language)
                .order_by(ConversationState.version.desc())
                .limit(1)
            )
            if (latest or 0) != base_version:  # somebody else stored a version meanwhile
                return Outcome(conversation_id, "no_new", llm_calls=out.llm_calls, llm_call_ids=out.llm_call_ids)
            row = ConversationState(
                conversation_id=conversation_id, language=language, version=base_version + 1, state_json=final,
                changed_since_last=final["changed_since_last"], covered_item_ids=covered_after,
                last_item_published_at=last_published, prompt_version=STATE_PROMPT_VERSION, model=model,
                llm_call_id=call_id,
            )
            if now is not None:
                row.created_at = now
            s.add(row)
            s.commit()
        except IntegrityError:
            s.rollback()
            return Outcome(conversation_id, "no_new", llm_calls=out.llm_calls, llm_call_ids=out.llm_call_ids)
    out.status = status
    out.code = None
    out.version = base_version + 1
    return out


def _build_sources(items: dict[int, Item], refs: list[str]) -> list[dict[str, Any]]:
    """ref -> url, date, author from the DB (never from the model, so it cannot invent links)."""
    out = []
    for ref in refs:
        item = items.get(int(ref[1:])) if re.fullmatch(r"m\d+", ref) else None
        if item is None:
            continue
        out.append(
            {
                "ref": ref,
                "url": item.url,
                "date": item.published_at.astimezone(UTC).isoformat(timespec="minutes"),
                "author_name": mask_addresses(item.author_name) if item.author_name else None,
            }
        )
    return out


# --- rendering ---------------------------------------------------------------------------------

_LABELS = {
    "en": {
        "issues": "Issues", "agreed": "Agreed", "open": "Open questions", "next": "Next",
        "changed": "Changed since last", "sources": "Sources", "version": "version",
        "status": {"agreed": "agreed", "leaning": "leaning", "open": "open", "parked": "parked"},
    },
    "ja": {
        "issues": "論点", "agreed": "合意済み", "open": "未決着", "next": "次", "changed": "前回から",
        "sources": "出典", "version": "版",
        "status": {"agreed": "合意", "leaning": "傾き", "open": "未決着", "parked": "保留"},
    },
}


def render_state(row: ConversationState) -> str:
    """Plain text for the CLI ``show``: in the stored language, refs resolved to urls at the end."""
    lab = _LABELS.get(row.language.split("-")[0].lower(), _LABELS["en"])
    st = row.state_json
    lines = [f"{st['status_line']}  ({lab['version']} {row.version}, {row.language})"]

    def refs(rs: list[str]) -> str:
        return f" [{', '.join(rs)}]" if rs else ""

    if st["issues"]:
        lines += ["", f"{lab['issues']}:"]
        for i in st["issues"]:
            lines.append(f"- ({lab['status'][i['status']]}) {i['issue']}{refs(i['refs'])}")
            for p in i["positions"]:
                lines.append(f"    {', '.join(p['by'])}: {p['stance']}{refs(p['refs'])}")
            if i["evidence"]:
                lines.append(f"    > {i['evidence']}")
    for key, name in (("agreed", "agreed"), ("open_questions", "open"), ("next", "next")):
        entries = st[key]
        if entries:
            lines += ["", f"{lab[name]}:"]
            lines += [f"- {e['text']}{refs(e['refs'])}" if isinstance(e, dict) else f"- {e}" for e in entries]
    if row.changed_since_last:
        lines += ["", f"{lab['changed']}: {row.changed_since_last}"]
    if st["sources"]:
        lines += ["", f"{lab['sources']}:"]
        for src in st["sources"]:
            lines.append(f"  {src['ref']}  {src['date'][:10]}  {src['author_name'] or '-'}  {src['url'] or '-'}")
    return "\n".join(lines)
