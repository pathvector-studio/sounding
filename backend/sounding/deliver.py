"""Delivery of a built digest (spec phase 3, R10): email (SMTP) and webhook (Discord / Slack compatible).

``web`` and ``atom`` need no sending: a digest is "delivered" there by being in the database.

* One ``deliveries`` row per (digest, channel): ``status`` pending | sent | failed, ``attempts``,
  ``last_attempt_at`` and ``error`` (a short fixed token such as ``http_404``, never server output).
* A channel already ``sent`` is never sent again (unless a person forces it with ``deliver --force``).
* A failed channel is tried once more on the next digest run (``MAX_ATTEMPTS`` = 2 in all). A person running
  ``deliver`` by hand is not held to that limit.
* An enabled channel that is empty or still ``change-me`` fails with ``not_configured``; it never stops the digest job.
* Secrets (SMTP user and password, the webhook URL, the addresses) are never logged, stored or put into an
  exception that leaves this module: errors are mapped to tokens and the original exception is dropped.

Webhook payload (see ``webhook_flavor``): Discord reads ``content`` and Slack reads ``text``. Discord and
Slack are said to ignore unknown keys, but that is not documented as a promise, so a known host gets only its own
key; any other host (a Slack/Discord-compatible bridge) gets both.
"""

from __future__ import annotations

import html
import logging
import smtplib
import socket
import ssl
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from sounding.config import Settings
from sounding.digest import EVENT_LABELS, _TEXT, _lang, _when, render_digest_text
from sounding.models import Delivery, Digest
from sounding.safe import clean_body, clean_url

log = logging.getLogger(__name__)
# httpx logs every request (with its URL) at INFO; the webhook URL is a credential.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

SessionFactory = Callable[[], Session]

SEND_CHANNELS = ("email", "webhook")  # the channels that need sending, in this order
KNOWN_CHANNELS = frozenset({"web", "email", "webhook", "atom"})
MAX_ATTEMPTS = 2  # the first try plus one retry on the next run
RETRY_WINDOW = timedelta(days=3)  # earlier digests older than this (by period end) are not retried automatically
NOTE_RESERVE = 20  # room for the "... +n / m" line
WEBHOOK_LIMIT = 1990  # Discord's content limit is 2000 characters
NO_CHANNELS = "no sending channel enabled (email, webhook)"
SENDERS: dict[str, Callable[[Settings, dict[str, Any], str, int], None]] = {}  # filled below


class DeliveryError(Exception):
    """A failed delivery. ``code`` is a fixed token that is safe to store and show."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- configuration -------------------------------------------------------------------------------

def _unset(value: str) -> bool:
    v = (value or "").strip().lower()
    return not v or "change-me" in v


def config_error(settings: Settings, channel: str) -> str | None:
    """``not_configured`` when the channel is enabled but its settings are empty or still the example values."""
    if channel == "webhook":
        url = settings.webhook_url.get_secret_value()
        return "not_configured" if _unset(url) or clean_url(url.strip()) is None else None
    if channel == "email":
        if _unset(settings.smtp_host) or _unset(settings.smtp_from.get_secret_value()) or _unset(settings.digest_to.get_secret_value()):
            return "not_configured"
        user = settings.smtp_user.get_secret_value().strip()
        if user and (_unset(user) or _unset(settings.smtp_password.get_secret_value())):
            return "not_configured"
        return None
    return None


# --- shared text ---------------------------------------------------------------------------------

def _shorten(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: max(n - 1, 0)].rstrip() + "…"


def digest_url(settings: Settings, digest_id: int) -> str:
    return f"{settings.public_base_url}/digests/{digest_id}"


def _period(body: dict[str, Any]) -> str:
    p = body["period"]
    return f"{_when(p['start'])} - {_when(p['end'])} ({p['tz']})"


# --- email ---------------------------------------------------------------------------------------

def _a(url: Any, label: str) -> str:
    safe = clean_url(url)
    text = html.escape(label)
    return f'<a href="{html.escape(safe, quote=True)}">{text}</a>' if safe else text


def render_digest_html(body: dict[str, Any], language: str) -> str:
    """HTML of a (cleaned) digest body: the same content as ``render_digest_text``, built with ``html.escape`` only."""
    t = _TEXT[_lang(language)]
    labels = EVENT_LABELS[_lang(language)]
    e = html.escape
    out = [
        '<!doctype html><html><body style="font-family:sans-serif;max-width:42em;line-height:1.5">',
        f"<h1>{e(t['title'])}</h1><p>{e(_period(body))}</p>",
    ]
    if body.get("health"):
        out.append(f"<h2>{e(t['health'])}</h2><ul>")
        out += [f"<li>{e(t.get(h['kind'], h['kind']).format(d=h.get('detail', '')))}</li>" for h in body["health"]]
        out.append("</ul>")
    if body.get("quiet"):
        out.append(f"<p>{e(t['quiet'])}</p>")
    if body.get("conversations"):
        out.append(f"<h2>{e(t['conversations'])}</h2>")
        for c in body["conversations"]:
            n = t["new1"] if c["new_messages"] == 1 and "new1" in t else t["new"].format(n=c["new_messages"])
            out.append(f"<h3>{_a(c.get('web_url'), c['subject'])}</h3><p><small>{e(c['list'])} · {e(n)}</small></p>")
            st = c.get("state")
            if st:
                out.append(f"<p>{e(st['status_line'])}</p>")
                if st.get("changed_since_last"):
                    out.append(f"<p><strong>{e(t['changed'])}:</strong> {e(st['changed_since_last'])}</p>")
            else:
                out.append(f"<p><em>{e(t['no_state'])}</em></p>")
            if c.get("participants"):
                out.append(f"<p><small>{e(t['participants'])}: {e(', '.join(c['participants']))}</small></p>")
            if clean_url(c.get("latest_url")):
                out.append(f"<p><small>{_a(c['latest_url'], t['latest'])}</small></p>")
    if body.get("events"):
        out.append(f"<h2>{e(t['events'])}</h2><ul>")
        for ev in body["events"]:
            ref = f" {ev['ref']}" if ev.get("ref") else ""
            label = f"{labels.get(ev['kind'], ev['kind'])}{ref}: {ev['title']}"
            out.append(f"<li>{_a(ev.get('url'), label)} <small>({e(_when(ev['occurred_at']))})</small></li>")
        out.append("</ul>")
    if body.get("items"):
        out.append(f"<h2>{e(t['items'])}</h2><ul>")
        for it in body["items"]:
            summary = f"<br>{e(it['summary'])}" if it.get("summary") else ""
            out.append(f"<li>{_a(it.get('url'), it['title'])}{summary}</li>")
        out.append("</ul>")
    out.append("</body></html>")
    return "".join(out)


def build_email(settings: Settings, body: dict[str, Any], language: str, digest_id: int) -> EmailMessage:
    """The message for a digest: plain text plus an HTML alternative. Raises ValueError for a bad header value."""
    body = clean_body(body)
    t = _TEXT[_lang(language)]
    recipients = [a.strip() for a in settings.digest_to.get_secret_value().split(",") if a.strip()]
    msg = EmailMessage()
    msg["Subject"] = f"{t['title']} — {_period(body)}"
    msg["From"] = settings.smtp_from.get_secret_value().strip()
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain="sounding.invalid")  # the default would carry this machine's host name
    text = render_digest_text(body, language) + f"\n\n{t['web']}: {digest_url(settings, digest_id)}\n"
    msg.set_content(text)
    link = _a(digest_url(settings, digest_id), t["web"])
    msg.add_alternative(render_digest_html(body, language).replace("</body>", f"<p>{link}</p></body>"), subtype="html")
    return msg


def _open_smtp(host: str, port: int, timeout: float, implicit_tls: bool, local_hostname: str) -> smtplib.SMTP:
    """Connect (tests replace this). Certificates are always verified."""
    if implicit_tls:
        return smtplib.SMTP_SSL(host, port, timeout=timeout, local_hostname=local_hostname, context=ssl.create_default_context())
    return smtplib.SMTP(host, port, timeout=timeout, local_hostname=local_hostname)


def _smtp_code(exc: BaseException) -> str:
    if isinstance(exc, DeliveryError):
        return exc.code
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, ssl.SSLError):
        return "tls_error"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "smtp_auth_failed"
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return "smtp_auth_unsupported"
    if isinstance(exc, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError)):
        return "smtp_refused"
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return "smtp_disconnected"
    if isinstance(exc, (smtplib.SMTPConnectError, OSError)):
        return "connect_error"
    if isinstance(exc, smtplib.SMTPException):
        return "smtp_error"
    if isinstance(exc, ValueError):
        return "invalid_message"
    return "error"


def send_email(settings: Settings, body: dict[str, Any], language: str, digest_id: int) -> None:
    """Send one digest by SMTP. Port 465 is TLS from the start; any other port must offer STARTTLS (no plaintext fallback)."""
    port = settings.smtp_port
    implicit = port == 465
    host_name = urlsplit(settings.public_base_url).hostname or "localhost"
    smtp: smtplib.SMTP | None = None
    try:
        msg = build_email(settings, body, language, digest_id)
        smtp = _open_smtp(settings.smtp_host.strip(), port, float(settings.external_timeout_seconds), implicit, host_name)
        smtp.ehlo()
        if not implicit:
            if not smtp.has_extn("starttls"):
                raise DeliveryError("starttls_unavailable")
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        user = settings.smtp_user.get_secret_value().strip()
        if user:
            smtp.login(user, settings.smtp_password.get_secret_value())
        smtp.send_message(msg)
    except Exception as exc:  # noqa: BLE001 - mapped to a token; the original (it may carry addresses) is dropped
        raise DeliveryError(_smtp_code(exc)) from None
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001
                try:
                    smtp.close()
                except Exception:  # noqa: BLE001
                    pass


# --- webhook -------------------------------------------------------------------------------------

HTTP_TRANSPORT: httpx.BaseTransport | None = None  # tests set a MockTransport

_WEBHOOK_TEXT = {
    "en": "Procedural updates: {e}, related articles: {i}",
    "ja": "手続きの更新 {e} 件、関連記事 {i} 件",
}


def webhook_flavor(url: str) -> str:
    """``discord``, ``slack`` or ``generic`` from the host of the URL."""
    host = (urlsplit(url).hostname or "").lower()
    if host in ("discord.com", "discordapp.com") or host.endswith((".discord.com", ".discordapp.com")):
        return "discord"
    if host == "hooks.slack.com":
        return "slack"
    return "generic"


def build_webhook_message(settings: Settings, body: dict[str, Any], language: str, digest_id: int, limit: int = WEBHOOK_LIMIT) -> str:
    """A compact message of at most ``limit`` characters that always ends with the link to the digest."""
    body = clean_body(body)
    lang = _lang(language)
    t = _TEXT[lang]
    head = [f"{t['title']}  {_period(body)}"]
    for h in body.get("health") or []:
        head.append("! " + _shorten(t.get(h["kind"], h["kind"]).format(d=h.get("detail", "")), 140))
    if body.get("quiet"):
        head.append(t["quiet"])
    counts = body.get("counts") or {}
    n_events = counts.get("events_total", len(body.get("events") or []))
    n_items = len(body.get("items") or [])
    footer_lines = []
    if n_events or n_items:
        footer_lines.append(_WEBHOOK_TEXT[lang].format(e=n_events, i=n_items))
    footer_lines.append(digest_url(settings, digest_id))
    footer = "\n".join(footer_lines)

    budget = limit - len(footer) - 1
    text = _shorten("\n".join(head), budget) if len("\n".join(head)) > budget else "\n".join(head)
    convs = body.get("conversations") or []
    shown = 0
    for c in convs:
        st = c.get("state")
        line = f"* {_shorten(c['subject'], 80)}"
        line += f" - {_shorten(st['status_line'], 160)}" if st else f" ({t['no_state']})"
        block = line + (f"\n  {c['web_url']}" if c.get("web_url") else "")
        if len(text) + 2 + len(block) + NOTE_RESERVE > budget:
            break
        text += "\n\n" + block
        shown += 1
    if shown < len(convs):
        note = f"... +{len(convs) - shown} / {len(convs)}"
        if len(text) + 2 + len(note) <= budget:
            text += "\n\n" + note
    return f"{text}\n{footer}"


def webhook_payload(url: str, message: str) -> dict[str, Any]:
    flavor = webhook_flavor(url)
    if flavor == "discord":
        return {"content": message, "allowed_mentions": {"parse": []}}  # a subject must not ping anyone
    if flavor == "slack":
        return {"text": message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")}
    return {"content": message, "text": message}


def send_webhook(settings: Settings, body: dict[str, Any], language: str, digest_id: int) -> None:
    url = settings.webhook_url.get_secret_value().strip()
    payload = webhook_payload(url, build_webhook_message(settings, body, language, digest_id))
    try:
        with httpx.Client(
            timeout=float(settings.external_timeout_seconds), follow_redirects=False, transport=HTTP_TRANSPORT
        ) as client:
            resp = client.post(url, json=payload)
    except httpx.TimeoutException:
        raise DeliveryError("timeout") from None
    except httpx.ConnectError:
        raise DeliveryError("connect_error") from None
    except (httpx.InvalidURL, httpx.UnsupportedProtocol):
        raise DeliveryError("invalid_url") from None
    except Exception:  # noqa: BLE001 - the message of an httpx error may contain the URL
        raise DeliveryError("http_error") from None
    if not 200 <= resp.status_code < 300:
        raise DeliveryError(f"http_{resp.status_code}")


SENDERS.update(email=send_email, webhook=send_webhook)


# --- recording -----------------------------------------------------------------------------------

def _attempt(settings: Settings, channel: str, body: dict[str, Any], language: str, digest_id: int) -> str | None:
    """None when sent, else the error token."""
    err = config_error(settings, channel)
    if err:
        return err
    try:
        SENDERS[channel](settings, body, language, digest_id)
    except DeliveryError as exc:
        return exc.code
    except Exception as exc:  # noqa: BLE001 - never let a channel stop the job; the type name only
        log.error("digest %d: %s delivery crashed: %s", digest_id, channel, type(exc).__name__)
        return "error"
    return None


def _deliver_one(
    session_factory: SessionFactory, settings: Settings, digest_id: int, channel: str,
    body: dict[str, Any], language: str, *, force: bool, honor_limit: bool,
) -> str:
    with session_factory() as s:
        row = s.scalar(select(Delivery).where(Delivery.digest_id == digest_id, Delivery.channel == channel))
        if row is None:
            row = Delivery(digest_id=digest_id, channel=channel, status="pending", attempts=0)
            s.add(row)
        elif row.status == "sent" and not force:
            return "already_sent"
        elif honor_limit and row.status != "sent" and (row.attempts or 0) >= MAX_ATTEMPTS:
            return f"gave_up({row.error or 'unknown'})"
        s.commit()
        row_id = row.id
    err = _attempt(settings, channel, body, language, digest_id)
    with session_factory() as s:
        row = s.get(Delivery, row_id)
        assert row is not None
        row.attempts = (row.attempts or 0) + 1
        row.last_attempt_at = datetime.now(UTC)
        row.status = "failed" if err else "sent"
        row.error = err
        s.commit()
    log.info("digest %d: %s delivery %s", digest_id, channel, f"failed ({err})" if err else "sent")
    return f"failed({err})" if err else "sent"


def deliver_digest(
    session_factory: SessionFactory, settings: Settings, digest_id: int, *,
    channels: set[str] | frozenset[str] | None = None, force: bool = False, honor_limit: bool = True,
) -> str:
    """Send a built digest to the enabled sending channels and return e.g. ``email=sent webhook=failed(http_404)``.

    ``channels`` narrows the channels; ``force`` resends one that is already ``sent``; ``honor_limit=False`` (a person
    at the CLI) ignores the attempts limit. Channels not in ``DELIVERY_CHANNELS`` are never used."""
    unknown = settings.enabled_channels - KNOWN_CHANNELS
    if unknown:
        log.warning("unknown DELIVERY_CHANNELS entries ignored: %s", ", ".join(sorted(unknown)))
    enabled = [c for c in SEND_CHANNELS if c in settings.enabled_channels and (channels is None or c in channels)]
    if not enabled:
        log.debug("digest %d: delivery skipped: %s", digest_id, NO_CHANNELS)
        return NO_CHANNELS
    with session_factory() as s:
        d = s.get(Digest, digest_id)
        if d is None or not (d.body_json or {}).get("period"):
            return "digest_not_found"
        body, language = dict(d.body_json), d.language
    return " ".join(
        f"{c}={_deliver_one(session_factory, settings, digest_id, c, body, language, force=force, honor_limit=honor_limit)}"
        for c in enabled
    )


def retry_failed(session_factory: SessionFactory, settings: Settings, current_digest_id: int) -> list[str]:
    """Try again the sending channels of earlier digests that failed fewer than ``MAX_ATTEMPTS`` times.

    Only digests whose period ended within ``RETRY_WINDOW`` of the current one (an old digest is not sent late by
    surprise). Returns summaries such as ``digest 3: email=sent``."""
    enabled = [c for c in SEND_CHANNELS if c in settings.enabled_channels]
    if not enabled:
        return []
    with session_factory() as s:
        current = s.get(Digest, current_digest_id)
        if current is None:
            return []
        rows = s.execute(
            select(Delivery.digest_id, Delivery.channel)
            .join(Digest, Digest.id == Delivery.digest_id)
            .where(
                Delivery.status != "sent", Delivery.attempts < MAX_ATTEMPTS, Delivery.digest_id != current_digest_id,
                Delivery.channel.in_(enabled), Digest.period_end >= current.period_end - RETRY_WINDOW,
            )
            .order_by(Delivery.digest_id, Delivery.channel)
        ).all()
    wanted: dict[int, set[str]] = {}
    for digest_id, channel in rows:
        wanted.setdefault(digest_id, set()).add(channel)
    return [f"digest {did}: {deliver_digest(session_factory, settings, did, channels=chs)}" for did, chs in wanted.items()]
