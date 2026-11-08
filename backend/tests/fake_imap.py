"""A fake IMAP server and client for adapter tests. Synthetic data only."""

from __future__ import annotations

import email
import imaplib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def make_mail(
    msgid: str | None,
    subject: str,
    when: datetime,
    *,
    sender: str = "Alice Example <alice.sender@example.com>",
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    body: str = "Plain body.",
    html: str | None = None,
    html_only: bool = False,
    auto: bool = False,
    archived_at: str | None = None,
    attachment: bool = False,
    charset: str = "utf-8",
) -> bytes:
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "list@example.org"
    m["Subject"] = subject
    m["Date"] = format_datetime(when)
    if msgid:
        m["Message-ID"] = f"<{msgid}>"
    if in_reply_to:
        m["In-Reply-To"] = f"<{in_reply_to}>"
    if references:
        m["References"] = " ".join(f"<{r}>" for r in references)
    if auto:
        m["Auto-Submitted"] = "auto-generated"
    if archived_at:
        m["Archived-At"] = f"<{archived_at}>"
    if html_only:
        m.set_content(html or "<p>HTML body</p>", subtype="html", charset=charset)
    else:
        m.set_content(body, charset=charset)
        if html:
            m.add_alternative(html, subtype="html")
    if attachment:
        m.add_attachment(b"attached-bytes", maintype="application", subtype="octet-stream", filename="a.bin")
    return m.as_bytes()


@dataclass
class Mailbox:
    uidvalidity: int = 1000
    messages: dict[int, tuple[bytes, datetime]] = field(default_factory=dict)
    next_uid: int = 1

    def add(self, raw: bytes, when: datetime) -> int:
        uid = self.next_uid
        self.next_uid += 1
        self.messages[uid] = (raw, when)
        return uid


@dataclass
class FakeServer:
    mailboxes: dict[str, Mailbox] = field(default_factory=dict)
    login_failures: int = 0  # the first N logins are refused
    connect_failures: int = 0  # the first N connections raise OSError
    disconnect_on_fetch: int | None = None  # the Nth UID FETCH drops the connection
    login_error_text: str = "login refused"
    commands: list[str] = field(default_factory=list)
    connections: int = 0
    passwords: list[str] = field(default_factory=list)
    fetch_calls: int = 0

    def add_list(self, name: str, uidvalidity: int = 1000) -> Mailbox:
        box = Mailbox(uidvalidity=uidvalidity)
        self.mailboxes[name] = box
        return box

    def factory(self, host: str, port: int, timeout: float) -> FakeIMAP:
        self.commands.append(f"CONNECT {host}:{port}")
        if self.connect_failures > 0:
            self.connect_failures -= 1
            raise OSError("connection refused")
        self.connections += 1
        return FakeIMAP(self)

    def cmds(self, prefix: str) -> list[str]:
        return [c for c in self.commands if c.startswith(prefix)]


def _parse_since(value: str) -> datetime:
    d, mon, y = value.split("-")
    return datetime(int(y), MONTHS.index(mon) + 1, int(d), tzinfo=UTC)


class FakeIMAP:
    def __init__(self, server: FakeServer):
        self.s = server
        self.box: Mailbox | None = None

    def login(self, user: str, password: str):
        self.s.commands.append(f"LOGIN {user}")
        self.s.passwords.append(password)
        if self.s.login_failures > 0:
            self.s.login_failures -= 1
            raise imaplib.IMAP4.error(f"{self.s.login_error_text} {password}")
        return "OK", [b"logged in"]

    def select(self, mailbox: str = "INBOX", readonly: bool = False):
        self.s.commands.append(f"{'EXAMINE' if readonly else 'SELECT'} {mailbox}")
        name = mailbox.strip('"').removeprefix("Shared Folders/")
        if not mailbox.startswith('"Shared Folders/') or name not in self.s.mailboxes:
            return "NO", [b"no such mailbox"]
        self.box = self.s.mailboxes[name]
        return "OK", [str(len(self.box.messages)).encode()]

    def response(self, code: str):
        assert self.box is not None
        if code == "UIDVALIDITY":
            return code, [str(self.box.uidvalidity).encode()]
        if code == "UIDNEXT":
            return code, [str(self.box.next_uid).encode()]
        return code, [None]

    def uid(self, command: str, *args: str):
        assert self.box is not None
        self.s.commands.append(f"UID {command} {' '.join(args)}")
        box = self.box
        if command == "SEARCH":
            if args[0] == "SINCE":
                since = _parse_since(args[1])
                hits = [u for u, (_, d) in box.messages.items() if d >= since]
            elif args[0] == "UID":
                lo = int(args[1].split(":")[0])
                top = max(box.messages, default=0)
                hits = [u for u in box.messages if u >= lo]
                if not hits and top:  # RFC 3501: "N:*" always includes the last message
                    hits = [top]
            elif args[0] == "HEADER":
                want = args[2].strip('"').lower()
                hits = []
                for u, (raw, _) in box.messages.items():
                    got = email.message_from_bytes(raw).get("Message-ID", "").lower()
                    if want in got:
                        hits.append(u)
            else:
                raise AssertionError(args)
            return "OK", [" ".join(map(str, sorted(hits))).encode()]
        if command == "FETCH":
            assert args[1] == "(UID BODY.PEEK[])", "must not mark messages as read"
            self.s.fetch_calls += 1
            if self.s.disconnect_on_fetch == self.s.fetch_calls:
                raise imaplib.IMAP4.abort("socket error: EOF")
            data: list = []
            for u in [int(x) for x in args[0].split(",")]:
                if u in box.messages:
                    raw = box.messages[u][0]
                    data.append((f"{u} (UID {u} BODY[] {{{len(raw)}}}".encode(), raw))
                    data.append(b")")
            return "OK", data
        raise AssertionError(command)

    def logout(self):
        return "BYE", [b""]
