"""Shared helpers for the phase-0 spike (standard library only)."""
from __future__ import annotations

import email
import email.policy
import email.utils
import json
import mailbox
import re
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
DATA = HERE / "data"
OUT = HERE / "out"
IMAP_DIR = DATA / "imap"
RSYNC_DIR = DATA / "rsync"


def load_imap_messages() -> dict:
    """uid -> (raw bytes, email.message.Message)"""
    res = {}
    for p in sorted(IMAP_DIR.glob("*.eml"), key=lambda p: int(p.stem)):
        raw = p.read_bytes()
        res[int(p.stem)] = (raw, email.message_from_bytes(raw, policy=email.policy.default))
    return res


def load_mbox_messages(path: Path) -> list:
    """list of (raw bytes, Message) from an mbox file."""
    box = mailbox.mbox(str(path), factory=None)
    out = []
    for m in box:
        raw = m.as_bytes()
        out.append((raw, email.message_from_bytes(raw, policy=email.policy.default)))
    return out


def msgid(msg) -> str | None:
    v = msg.get("Message-ID")
    if not v:
        return None
    m = re.search(r"<([^>]+)>", str(v))
    return (m.group(1) if m else str(v).strip()).strip().lower() or None


def msg_date(msg):
    try:
        d = email.utils.parsedate_to_datetime(str(msg.get("Date")))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1))
