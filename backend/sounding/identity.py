"""Sender identity: a display name and a keyed hash. The address itself is never stored or logged.

``author_key = "mail:" + first 32 hex of HMAC-SHA256(IDENTITY_SECRET, lower-cased address)``.
Changing the secret changes every key. If the secret is empty (not configured), no key can be
made and ``author_key_for_email`` returns None; the display name is still available.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from email.header import decode_header, make_header
from email.utils import parseaddr


def author_key_for_email(address: str | None, secret: str | None) -> str | None:
    """Stable key for an address (case and surrounding spaces ignored), or None without a secret."""
    addr = (address or "").strip().lower()
    if not addr or not secret:
        return None
    digest = hmac.new(secret.encode(), addr.encode(), hashlib.sha256).hexdigest()
    return "mail:" + digest[:32]


def _decode(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - undecodable header: use it as is
        return value


def display_name_for(from_header_value: str | None) -> str | None:
    """The display name, else the local part masked as ``a…``. Never the full address."""
    name, addr = parseaddr(str(from_header_value or ""))
    name = _decode(name).strip().strip("\"'").strip()
    if name and "@" not in name:
        return name
    if not addr:  # unparsable (e.g. an address used as its own display name): look for one
        m = re.search(r"[^\s<>\"']+@[^\s<>\"']+", str(from_header_value or ""))
        addr = m.group(0) if m else ""
    local = addr.strip().split("@", 1)[0].strip()
    if not local:
        return None
    return local[0] + "…"


def sender_identity(from_header_value: str | None, secret: str | None) -> tuple[str | None, str | None]:
    """``(author_name, author_key)`` for a From header value."""
    _, addr = parseaddr(str(from_header_value or ""))
    return display_name_for(from_header_value), author_key_for_email(addr, secret)
