#!/usr/bin/env python3
"""Phase-0 spike: polite anonymous IMAP fetch of the last 30 days of one IETF list.

Standard library only. The contact address (FETCH_CONTACT) is the anonymous
password; it comes from the environment or the FETCH_CONTACT= line of ./.env
and is never printed or written anywhere.
"""
from __future__ import annotations

import imaplib
import json
import os
import re
import ssl
import sys
import time
from datetime import date, timedelta
from pathlib import Path

HOST = "imap.ietf.org"
PORT = 993
LIST_NAME = "sidrops"
DAYS = 30
BATCH = 25
PAUSE = 1.2  # seconds between requests (spec: >= 1s)
OUT = Path(__file__).parent / "data" / "imap"
SECRET_VALUES: list = []


def load_contact() -> str:
    v = os.environ.get("FETCH_CONTACT", "").strip()
    if not v:
        env = Path(".env")
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("FETCH_CONTACT="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not v or v in ("change-me", "you@example.com"):
        sys.exit("FETCH_CONTACT is not set (env var or .env)")
    SECRET_VALUES.append(v)
    return v


def scrub(s) -> str:
    s = str(s)
    for sv in SECRET_VALUES:
        s = s.replace(sv, "<contact>")
    return s


def dec(x) -> str:
    return x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x)


def main() -> int:
    contact = load_contact()
    OUT.mkdir(parents=True, exist_ok=True)
    meta = {"host": HOST, "list": LIST_NAME, "errors": [], "timings": {}, "login": None}
    since = date.today() - timedelta(days=DAYS)
    since_s = since.strftime("%d-%b-%Y")
    meta["since"] = since.isoformat()
    t0 = time.time()

    def save_meta():
        (OUT / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    try:
        M = imaplib.IMAP4_SSL(HOST, PORT, ssl_context=ssl.create_default_context(), timeout=60)
    except Exception as e:  # noqa: BLE001
        meta["errors"].append("connect: " + scrub(e))
        save_meta()
        print("connect failed:", scrub(e))
        return 1
    meta["timings"]["connect_s"] = round(time.time() - t0, 2)
    meta["capabilities_pre_login"] = [dec(c) for c in M.capabilities]
    try:
        M.send(b"") if False else None
        imaplib.Commands["ID"] = ("NONAUTH", "AUTH", "SELECTED")
        typ, data = M._simple_command(
            "ID",
            '("name" "sounding-spike" "version" "0" '
            '"support-url" "https://github.com/pathvector-studio/sounding")',
        )
        meta["id_response"] = [typ, [dec(d) for d in (M.untagged_responses.get("ID") or [])]]
    except Exception as e:  # noqa: BLE001
        meta["id_response"] = "failed: " + scrub(e)

    t1 = time.time()
    try:
        typ, data = M.login("anonymous", contact)
        meta["login"] = {"ok": True, "response": [dec(d) for d in data]}
    except Exception as e:  # noqa: BLE001
        meta["login"] = {"ok": False, "response": scrub(e)}
        print("anonymous login FAILED. server said:", scrub(e))
        save_meta()
        return 2
    meta["timings"]["login_s"] = round(time.time() - t1, 2)
    meta["capabilities_after_login"] = [dec(c) for c in M.capabilities]
    try:
        typ, data = M.capability()
        meta["capabilities_after_login"] = dec(data[0]).split()
    except Exception as e:  # noqa: BLE001
        meta["errors"].append("capability: " + scrub(e))
    print("login ok; capabilities:", " ".join(meta["capabilities_after_login"]))
    time.sleep(PAUSE)

    # LIST to confirm the mailbox name
    typ, data = M.list('""', f'"*{LIST_NAME}*"')
    meta["list_response"] = [dec(d) for d in data if d]
    print("LIST:", meta["list_response"])
    mailbox = LIST_NAME
    for d in data:
        m = re.search(r'"([^"]*)"\s*$|\s(\S+)\s*$', dec(d))
        if m and LIST_NAME in (m.group(1) or m.group(2) or ""):
            mailbox = m.group(1) or m.group(2)
            break
    meta["mailbox"] = mailbox
    time.sleep(PAUSE)

    typ, data = M.select(f'"{mailbox}"', readonly=True)
    meta["select_response"] = [dec(d) for d in data]
    ur = M.untagged_responses
    meta["uidvalidity"] = [dec(x) for x in ur.get("UIDVALIDITY", [])]
    meta["exists"] = [dec(x) for x in ur.get("EXISTS", [])]
    meta["uidnext"] = [dec(x) for x in ur.get("UIDNEXT", [])]
    meta["highestmodseq"] = [dec(x) for x in ur.get("HIGHESTMODSEQ", [])]
    print("SELECT:", typ, meta["uidvalidity"], meta["exists"])
    time.sleep(PAUSE)

    typ, data = M.uid("SEARCH", None, "SINCE", since_s)
    uids = [int(x) for x in dec(data[0]).split()] if data and data[0] else []
    meta["uids"] = uids
    print("UIDs since", since_s, ":", len(uids))
    time.sleep(PAUSE)

    # server-side threading
    try:
        t2 = time.time()
        typ, data = M.uid("THREAD", "REFERENCES", "UTF-8", "SINCE", since_s)
        (OUT / "thread_references.txt").write_text("\n".join(dec(d) for d in data if d))
        meta["timings"]["thread_s"] = round(time.time() - t2, 2)
        meta["thread_response_type"] = typ
    except Exception as e:  # noqa: BLE001
        meta["errors"].append("thread: " + scrub(e))
        print("THREAD failed:", scrub(e))
    time.sleep(PAUSE)

    t3 = time.time()
    saved = 0
    for i in range(0, len(uids), BATCH):
        chunk = uids[i : i + BATCH]
        try:
            typ, data = M.uid("FETCH", ",".join(map(str, chunk)), "(UID BODY.PEEK[])")
        except Exception as e:  # noqa: BLE001
            meta["errors"].append(f"fetch batch {i}: {scrub(e)}")
            print("fetch error:", scrub(e))
            break
        for item in data:
            if isinstance(item, tuple):
                m = re.search(rb"UID (\d+)", item[0])
                if m:
                    (OUT / f"{int(m.group(1))}.eml").write_bytes(item[1])
                    saved += 1
        print(f"fetched {min(i + BATCH, len(uids))}/{len(uids)}")
        time.sleep(PAUSE)
    meta["timings"]["fetch_s"] = round(time.time() - t3, 2)
    meta["saved"] = saved
    meta["timings"]["total_s"] = round(time.time() - t0, 2)
    try:
        M.logout()
    except Exception:  # noqa: BLE001
        pass
    save_meta()
    print("saved", saved, "messages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
