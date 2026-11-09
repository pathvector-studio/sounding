"""Password hashing, session cookies, the Atom token and the login throttle (spec phase 3, R8, R9).

Standard library only (hashlib.scrypt, hmac). Nothing here imports FastAPI, so the ``hash-password``
CLI command can use it without loading the web stack.

* ``WEB_PASSWORD_HASH`` = ``scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>`` (standard base64, no padding).
* Session cookie = ``<base64url(json {"iat","exp"})>.<base64url(HMAC-SHA256(SESSION_SECRET, prefix + payload))>``.
* Atom token = ``hex(HMAC-SHA256(SESSION_SECRET, b"sounding-atom-feed-v1"))[:32]``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from collections import deque
from collections.abc import Callable

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
SCRYPT_MAX_N = 2**20  # a hash in the environment asking for more is refused (it is a memory bomb)
SALT_BYTES = 16
KEY_BYTES = 32
MIN_SECRET_BYTES = 32
SESSION_COOKIE = "sounding_session"
SESSION_LIFETIME = 30 * 24 * 3600
_SESSION_PREFIX = b"sounding-session-v1."
_FEED_MESSAGE = b"sounding-atom-feed-v1"


class WebConfigError(Exception):
    """The web UI cannot start with this configuration. The message never contains a secret value."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)


def _maxmem(n: int, r: int, p: int) -> int:
    return 128 * n * r * 2 + 128 * r * p * 2 + 1024 * 1024


# --- password -----------------------------------------------------------------------------------

def hash_password(password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else os.urandom(SALT_BYTES)
    key = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=KEY_BYTES, maxmem=_maxmem(n, r, p))
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(key)}"


def parse_hash(stored: str) -> tuple[int, int, int, bytes, bytes]:
    """The parts of a stored hash. Raises ValueError (without the value in the message) if malformed."""
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        raise ValueError("not a scrypt$n$r$p$salt$hash string")
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, key = _unb64(parts[4]), _unb64(parts[5])
    except (ValueError, binascii.Error):
        raise ValueError("not a scrypt$n$r$p$salt$hash string") from None
    if not (2 <= n <= SCRYPT_MAX_N and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 16 and salt and key):
        raise ValueError("scrypt parameters out of range")
    return n, r, p, salt, key


def verify_password(password: str, stored: str) -> bool:
    try:
        n, r, p, salt, key = parse_hash(stored)
        got = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=len(key), maxmem=_maxmem(n, r, p))
    except ValueError:
        return False
    return hmac.compare_digest(got, key)


# --- startup check ------------------------------------------------------------------------------

def validate_web_config(secret: str, password_hash: str) -> None:
    """Raise ``WebConfigError`` unless SESSION_SECRET is real (>= 32 bytes, not a placeholder) and the hash, if any, parses."""
    if "change-me" in secret:
        raise WebConfigError("SESSION_SECRET is still a change-me placeholder (generate one: openssl rand -hex 32)")
    if len(secret.encode()) < MIN_SECRET_BYTES:
        raise WebConfigError(f"SESSION_SECRET must be at least {MIN_SECRET_BYTES} bytes (generate one: openssl rand -hex 32)")
    if password_hash:
        try:
            parse_hash(password_hash)
        except ValueError as exc:
            raise WebConfigError(
                f"WEB_PASSWORD_HASH is not valid ({exc}); create it with `python -m sounding hash-password`. "
                "In an .env file for compose, put the value in single quotes or write each $ as $$"
            ) from None


# --- session cookie -----------------------------------------------------------------------------

def _mac(secret: str, payload: bytes) -> str:
    return base64.urlsafe_b64encode(hmac.new(secret.encode(), _SESSION_PREFIX + payload, hashlib.sha256).digest()).decode().rstrip("=")


def make_session(secret: str, *, now: float | None = None, lifetime: int = SESSION_LIFETIME) -> str:
    now = int(time.time() if now is None else now)
    payload = base64.urlsafe_b64encode(json.dumps({"iat": now, "exp": now + lifetime}, separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{payload}.{_mac(secret, payload.encode())}"


def verify_session(secret: str, cookie: str | None, *, now: float | None = None) -> bool:
    if not cookie or cookie.count(".") != 1:
        return False
    payload, mac = cookie.split(".")
    if not hmac.compare_digest(mac.encode(), _mac(secret, payload.encode()).encode()):
        return False
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        iat, exp = int(data["iat"]), int(data["exp"])
    except (ValueError, KeyError, TypeError, binascii.Error):
        return False
    now = time.time() if now is None else now
    return iat <= now + 300 and now < exp  # 5 minutes of clock skew allowed for iat


# --- Atom token ---------------------------------------------------------------------------------

def feed_token(secret: str) -> str:
    return hmac.new(secret.encode(), _FEED_MESSAGE, hashlib.sha256).hexdigest()[:32]


def feed_token_ok(secret: str, token: str) -> bool:
    return hmac.compare_digest(token.encode(), feed_token(secret).encode())


# --- login throttle -----------------------------------------------------------------------------

class LoginThrottle:
    """In-memory, per client address. Choice: 429 with ``Retry-After`` (no sleeping, so no worker thread is held).

    Failures older than ``window`` (15 minutes) are forgotten. After ``free`` (5) failures inside the window every
    further attempt is refused until ``min(2 ** (n - free + 1), cap)`` seconds (2, 4, 8, ... capped at 15 minutes)
    after the latest failure, where n is the number of failures in the window. A success forgets the address.
    The password is not even checked while refused, so a flood costs no scrypt time. Behind a reverse proxy that
    does not forward the client address everyone shares one counter; that is the safe side of the trade-off.
    """

    def __init__(self, *, free: int = 5, window: float = 900.0, cap: float = 900.0,
                 clock: Callable[[], float] = time.monotonic, max_entries: int = 10_000) -> None:
        self.free, self.window, self.cap, self.clock, self.max_entries = free, window, cap, clock, max_entries
        self._fails: dict[str, deque[float]] = {}

    def _recent(self, key: str, now: float) -> deque[float]:
        q = self._fails.get(key)
        if q is None:
            return deque()
        while q and now - q[0] > self.window:
            q.popleft()
        if not q:
            del self._fails[key]
        return q

    def retry_after(self, key: str) -> int:
        """Seconds the caller must wait (0 = may try)."""
        now = self.clock()
        q = self._recent(key, now)
        if len(q) < self.free:
            return 0
        wait = min(2 ** (len(q) - self.free + 1), self.cap) - (now - q[-1])
        return max(0, int(wait) + (1 if wait % 1 else 0))

    def failure(self, key: str) -> None:
        now = self.clock()
        if key not in self._fails and len(self._fails) >= self.max_entries:
            for k in list(self._fails):  # drop expired entries; if still full, drop the oldest one
                self._recent(k, now)
            if len(self._fails) >= self.max_entries:
                self._fails.pop(next(iter(self._fails)))
        self._fails.setdefault(key, deque()).append(now)

    def success(self, key: str) -> None:
        self._fails.pop(key, None)
