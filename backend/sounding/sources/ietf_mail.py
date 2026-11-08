"""IETF mailing lists over anonymous IMAP (specs/phase1.md, "IETF ML（IMAP）").

``imap.ietf.org:993`` (TLS), user ``anonymous``, password = FETCH_CONTACT, mailbox
``Shared Folders/<list>`` opened read-only (EXAMINE) and read with ``BODY.PEEK``.

Everything IETF-specific lives here. Secrets (FETCH_CONTACT, IDENTITY_SECRET) and From addresses
are never logged and never put in an exception message; failures are reported by type name and a
fixed token only.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import re
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, ClassVar

from sounding.classify import classify_auto
from sounding.config import Settings
from sounding.conversations import normalize_msgid
from sounding.identity import sender_identity
from sounding.models import Source
from sounding.sources.base import ConversationHints, FetchResult, Item, SourceError

log = logging.getLogger(__name__)

IMAP_PORT = 993
IMAP_USER = "anonymous"
MAILBOX_PREFIX = "Shared Folders/"
BATCH_SIZE = 50
BATCH_PAUSE_SECONDS = 1.0
RETRY_PAUSE_SECONDS = 30.0

LIST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_ANGLE_RE = re.compile(r"<([^<>\s]+)>")
_UID_RE = re.compile(rb"\bUID\s+(\d+)", re.IGNORECASE)
_SAFE_MSGID_RE = re.compile(r"^[\x21-\x7e]+$")  # printable ASCII, no spaces

# Failures that mean "the connection or the command failed" (not "the mailbox does not exist").
_IMAP_ERRORS = (imaplib.IMAP4.error, OSError, EOFError)

ClientFactory = Callable[[str, int, float], Any]


def _default_factory(host: str, port: int, timeout: float) -> Any:
    return imaplib.IMAP4_SSL(host, port, timeout=timeout)


def imap_date(day: datetime) -> str:
    """``01-Oct-2026`` (English month names regardless of locale)."""
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


class _TextExtractor(HTMLParser):
    _BLOCK = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "table"}
    _SKIP = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 - broken markup: keep what was extracted so far
        pass
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in "".join(parser.parts).split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def _part_text(part: EmailMessage) -> str:
    try:
        return str(part.get_content())
    except Exception:  # noqa: BLE001 - unknown charset etc.: decode by hand
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")


def extract_body(msg: EmailMessage) -> str:
    """text/plain preferred, else text/html turned into text. Attachments are ignored."""
    plain: EmailMessage | None = None
    html: EmailMessage | None = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = str(part.get("Content-Disposition", "")).split(";", 1)[0].strip().lower()
        if disposition == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            plain = part
        elif ctype == "text/html" and html is None:
            html = part
    if plain is not None:
        return _part_text(plain).replace("\r\n", "\n").strip()
    if html is not None:
        return html_to_text(_part_text(html))
    return ""


def _header(msg: EmailMessage, name: str) -> str:
    try:
        value = msg.get(name)
    except Exception:  # noqa: BLE001 - malformed header
        return ""
    return str(value) if value is not None else ""


def _published_at(msg: EmailMessage, fallback: datetime) -> datetime:
    try:
        parsed = parsedate_to_datetime(_header(msg, "Date"))
    except Exception:  # noqa: BLE001
        return fallback
    if parsed is None:
        return fallback
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def parse_message(raw: bytes, *, uid: int, uidvalidity: int, secret: str | None, fallback_date: datetime) -> Item:
    """One RFC 822 message -> ``Item``. The From address is hashed and discarded."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)
    message_id = normalize_msgid(_header(msg, "Message-ID"))
    in_reply_to = normalize_msgid(_header(msg, "In-Reply-To"))
    references = tuple(
        dict.fromkeys(n for n in (normalize_msgid(m) for m in _ANGLE_RE.findall(_header(msg, "References"))) if n)
    )
    archived = _header(msg, "Archived-At").strip().strip("<>").strip()
    url = archived if archived.lower().startswith(("http://", "https://")) else None
    author_name, author_key = sender_identity(_header(msg, "From"), secret)
    reason = classify_auto(msg)
    title = re.sub(r"\s+", " ", _header(msg, "Subject")).strip() or "(no subject)"
    return Item(
        external_id=message_id or f"uid:{uidvalidity}:{uid}",
        url=url,
        title=title,
        author_name=author_name,
        author_key=author_key,
        published_at=_published_at(msg, fallback_date),
        body_text=extract_body(msg),
        conversation_hints=ConversationHints(message_id=message_id, in_reply_to=in_reply_to, references=references),
        is_auto=reason is not None,
        auto_reason=reason,
    )


def parse_fetch_response(data: Sequence[Any]) -> list[tuple[int | None, bytes]]:
    """imaplib ``UID FETCH (UID BODY.PEEK[])`` data -> ``[(uid, raw message)]``.

    The UID may sit in the header part or (some servers) in the trailing part after the literal.
    """
    out: list[tuple[int | None, bytes]] = []
    for part in data:
        if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
            head = part[0] if isinstance(part[0], (bytes, bytearray)) else b""
            m = _UID_RE.search(bytes(head))
            out.append((int(m.group(1)) if m else None, bytes(part[1])))
        elif isinstance(part, (bytes, bytearray)) and out and out[-1][0] is None:
            m = _UID_RE.search(bytes(part))
            if m:
                out[-1] = (int(m.group(1)), out[-1][1])
    return out


class IetfMailAdapter:
    kind: ClassVar[str] = "ietf_mail"

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: ClientFactory | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._factory = client_factory or _default_factory
        self._sleep = sleep
        self._clock = clock
        self._conn: Any | None = None

    # -- connection ------------------------------------------------------------------------

    def _connect(self) -> Any:
        if self._conn is not None:
            return self._conn
        last: Exception | None = None
        for attempt in (1, 2):
            client = None
            try:
                client = self._factory(
                    self._settings.ietf_imap_host, IMAP_PORT, float(self._settings.external_timeout_seconds)
                )
                client.login(IMAP_USER, self._settings.fetch_contact.get_secret_value())
                self._conn = client
                return client
            except _IMAP_ERRORS as exc:
                last = exc
                self._close_quietly(client)
                log.warning("imap connect/login failed (attempt %d): %s", attempt, type(exc).__name__)
                if attempt == 1:
                    self._sleep(RETRY_PAUSE_SECONDS)
        raise SourceError("connect_failed", f"IMAP connect/login failed ({type(last).__name__})") from None

    @staticmethod
    def _close_quietly(client: Any) -> None:
        if client is None:
            return
        try:
            client.logout()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        conn, self._conn = self._conn, None
        self._close_quietly(conn)

    def _drop(self) -> None:
        self.close()

    # -- commands --------------------------------------------------------------------------

    @staticmethod
    def _int_response(conn: Any, code: str) -> int | None:
        try:
            _, data = conn.response(code)
            return int(data[-1]) if data and data[-1] is not None else None
        except (ValueError, TypeError, IndexError):
            return None

    def _examine(self, conn: Any, list_name: str) -> tuple[int, int | None] | None:
        """(UIDVALIDITY, UIDNEXT) of the list's mailbox, or None when it does not exist."""
        typ, _ = conn.select(f'"{MAILBOX_PREFIX}{list_name}"', readonly=True)
        if typ != "OK":
            return None
        uidvalidity = self._int_response(conn, "UIDVALIDITY")
        if uidvalidity is None:
            raise SourceError("no_uidvalidity", "server did not report UIDVALIDITY")
        return uidvalidity, self._int_response(conn, "UIDNEXT")

    @staticmethod
    def _uid_command(conn: Any, *args: str) -> list[Any]:
        typ, data = conn.uid(*args)
        if typ != "OK":
            raise imaplib.IMAP4.error("command refused")
        return list(data or [])

    def _search(self, conn: Any, *criteria: str) -> list[int]:
        data = self._uid_command(conn, "SEARCH", *criteria)
        raw = data[0] if data and isinstance(data[0], (bytes, bytearray)) else b""
        return sorted({int(tok) for tok in raw.split() if tok.isdigit()})

    # -- SourceAdapter ---------------------------------------------------------------------

    def fetch(self, source: Source, cursor: dict[str, Any] | None) -> FetchResult:
        list_name = source.key
        if not LIST_NAME_RE.match(list_name):
            raise SourceError("invalid_list_name", "list name has unexpected characters")
        cursor = dict(cursor or {})
        missing = list(cursor.get("missing_ids") or [])
        conn = self._connect()
        try:
            opened = self._examine(conn, list_name)
        except _IMAP_ERRORS as exc:
            self._drop()
            raise SourceError("imap_error", f"IMAP error ({type(exc).__name__})") from None
        if opened is None:
            return FetchResult(items=[], cursor=cursor or None, notes=("not_found",))
        uidvalidity, uidnext = opened

        notes: list[str] = []
        old_validity, last_uid = cursor.get("uidvalidity"), cursor.get("last_uid")
        first_run = old_validity != uidvalidity or last_uid is None
        if old_validity is not None and old_validity != uidvalidity:
            notes.append("uidvalidity_changed")

        try:
            if first_run:
                since = self._clock() - timedelta(days=self._settings.ietf_backfill_days)
                uids = self._search(conn, "SINCE", imap_date(since))
            else:
                # "N:*" returns the newest message even when N is beyond it, hence the filter.
                uids = [u for u in self._search(conn, "UID", f"{last_uid + 1}:*") if u > last_uid]
        except _IMAP_ERRORS as exc:
            self._drop()
            raise SourceError("imap_error", f"IMAP error ({type(exc).__name__})") from None

        cap = self._settings.ietf_max_messages_per_run
        if len(uids) > cap:
            uids = uids[:cap]
            notes.append("truncated")

        items: list[Item] = []
        processed_uid: int | None = None
        error: str | None = None
        skipped = 0
        secret = self._settings.identity_secret.get_secret_value() or None
        for start in range(0, len(uids), BATCH_SIZE):
            batch = uids[start : start + BATCH_SIZE]
            if start:
                self._sleep(BATCH_PAUSE_SECONDS)
            try:
                data = self._uid_command(conn, "FETCH", ",".join(map(str, batch)), "(UID BODY.PEEK[])")
            except _IMAP_ERRORS as exc:
                error = "disconnected" if isinstance(exc, (OSError, EOFError, imaplib.IMAP4.abort)) else "imap_error"
                log.warning("imap fetch stopped for list %s: %s", list_name, type(exc).__name__)
                self._drop()
                break
            for uid, raw in parse_fetch_response(data):
                if uid is None or uid not in batch:
                    skipped += 1
                    continue
                try:
                    items.append(
                        parse_message(raw, uid=uid, uidvalidity=uidvalidity, secret=secret, fallback_date=self._clock())
                    )
                except Exception as exc:  # noqa: BLE001 - one bad message must not stop the list
                    skipped += 1
                    log.warning("unparseable message in list %s: %s", list_name, type(exc).__name__)
            processed_uid = batch[-1]
        if skipped:
            notes.append(f"skipped_unparseable:{skipped}")

        if processed_uid is not None:
            new_last: int | None = processed_uid
        elif first_run:
            new_last = uidnext - 1 if uidnext else None
            if error:  # nothing fetched at all: stay in "first run" mode
                new_last = None
        else:
            new_last = last_uid
        new_cursor = {"uidvalidity": uidvalidity, "last_uid": new_last, "missing_ids": missing}
        log.info("list %s: %d messages fetched%s", list_name, len(items), f" ({error})" if error else "")
        return FetchResult(items=items, cursor=new_cursor, notes=tuple(notes), error=error)

    def fetch_by_ids(self, source: Source, message_ids: Sequence[str]) -> list[Item]:
        """Messages of the list's mailbox with these (normalized) Message-IDs. Unknown ids are absent.

        Raises ``SourceError`` when the lookup could not be completed (so callers do not mistake a
        dropped connection for "not found").
        """
        list_name = source.key
        if not LIST_NAME_RE.match(list_name):
            raise SourceError("invalid_list_name", "list name has unexpected characters")
        wanted = {m for m in message_ids if m and _SAFE_MSGID_RE.match(m) and '"' not in m and "\\" not in m}
        if not wanted:
            return []
        conn = self._connect()
        secret = self._settings.identity_secret.get_secret_value() or None
        found: list[Item] = []
        try:
            opened = self._examine(conn, list_name)
            if opened is None:
                return []
            uidvalidity, _ = opened
            for mid in sorted(wanted):
                uids = self._search(conn, "HEADER", "Message-ID", f'"<{mid}>"')
                for uid in uids[:5]:
                    data = self._uid_command(conn, "FETCH", str(uid), "(UID BODY.PEEK[])")
                    for got_uid, raw in parse_fetch_response(data):
                        try:
                            item = parse_message(
                                raw, uid=got_uid or uid, uidvalidity=uidvalidity, secret=secret,
                                fallback_date=self._clock(),
                            )
                        except Exception as exc:  # noqa: BLE001
                            log.warning("unparseable ancestor in list %s: %s", list_name, type(exc).__name__)
                            continue
                        if item.conversation_hints and item.conversation_hints.message_id == mid:
                            found.append(item)
        except _IMAP_ERRORS as exc:
            self._drop()
            raise SourceError("imap_error", f"IMAP error ({type(exc).__name__})") from None
        return found
