#!/usr/bin/env python3
"""Measure the six preprocessing stages on the saved IMAP messages (offline).

Outputs: spike/out/preprocess.csv, spike/out/preprocess.md (numbers only),
spike/data/preprocess_per_message.json (input for estimate.py),
spike/data/samples/ (20 random before/after pairs, gitignored, for human review).
Tokens are estimated as chars / 4 (no tokenizer in the stdlib).
Reusable core: the functions strip_quotes / strip_signature / strip_footer take text
and a rules object; the IETF-specific patterns live in RULES.
"""
from __future__ import annotations

import csv
import html
import json
import random
import re
from html.parser import HTMLParser

from common import DATA, OUT, load_imap_messages, msg_date, write_json

# --- IETF/list specific knowledge, kept as configuration -------------------
RULES = {
    # paragraphs containing any of these are dropped (Note Well, legal footers)
    "boilerplate_paragraph": [
        r"note well", r"this is a reminder of ietf policies", r"\bBCP ?(78|79)\b.*contribut",
        r"intended recipient.*(confidential|privileged)", r"confidential.*intended recipient",
    ],
    # mailman footer: line of underscores followed shortly by "... mailing list"
    "mailman_footer_after": r"mailing list|mailman/listinfo|to unsubscribe send",
    # auto-generated mail
    "auto_sender": r"(noreply|no-reply|do-not-reply|internet-drafts@|datatracker|ietf-secretariat|"
                   r"agenda@|minutes@|meetecho|notifications?@|autoreply)",
    "auto_subject": r"^\s*(\[[^\]]*\]\s*)*(i-d action|new version notification|"
                    r"(automatic )?(out of office|autoreply|auto-reply)|"
                    r"(draft )?(agenda|minutes)\b.*(posted|uploaded|available)|"
                    r"(wg|document|protocol) action|last call:|undeliverable|delivery status)",
}

closers = r"(thanks|thank you|many thanks|regards|best regards|kind regards|best|cheers|sincerely|br|-- ?)[,!.]?"


class _H2T(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if tag in ("br", "p", "div", "tr", "li"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1

    def handle_data(self, d):
        if not self.skip:
            self.out.append(d)


def html_to_text(s: str) -> str:
    p = _H2T()
    p.feed(s)
    return html.unescape("".join(p.out))


def body_only(msg) -> str:
    """Stage 2: decoded text/plain parts that are not attachments (HTML as fallback)."""
    plain, htmls = [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        if part.get_content_disposition() == "attachment" or ctype.startswith("application/"):
            continue
        if ctype == "text/plain":
            try:
                plain.append(part.get_content())
            except Exception:  # noqa: BLE001
                plain.append(part.get_payload(decode=True).decode("utf-8", "replace"))
        elif ctype == "text/html":
            try:
                htmls.append(html_to_text(part.get_content()))
            except Exception:  # noqa: BLE001
                pass
    txt = "\n".join(plain) if plain else "\n".join(htmls)
    return txt.replace("\r\n", "\n").replace("\r", "\n")


def strip_quotes(text: str) -> str:
    lines = text.split("\n")
    # Outlook-style headers and "Original Message": cut everything from there on
    for i, l in enumerate(lines):
        if re.match(r"^\s*-{3,}\s*(original message|forwarded message)\s*-{3,}", l, re.I):
            lines = lines[:i]
            break
        if re.match(r"^\s*(From|发件人|寄件者|De|Von)\s*[:：]\s", l) and any(
            re.match(r"^\s*(Sent|Date|发送时间|日期|Envoy)", x) for x in lines[i + 1 : i + 5]
        ) and any(re.match(r"^\s*(To|Subject|收件人|主题|主旨|À|An)\s*[:：]", x) for x in lines[i + 1 : i + 6]):
            j = i
            while j > 0 and re.match(r"^\s*_{5,}\s*$", lines[j - 1]):
                j -= 1
            lines = lines[:j]
            break
    drop = set()
    for i, l in enumerate(lines):
        if re.match(r"^\s*>", l):
            drop.add(i)
        elif re.search(r"(wrote|writes|said):\s*$|a écrit\s*:\s*$|schrieb.*:\s*$", l.strip(), re.I):
            drop.add(i)
            # attribution may wrap over up to 2 earlier lines starting with "On "
            for k in range(1, 3):
                if i - k >= 0 and re.match(r"^\s*(On|Am|Le|El)\s", lines[i - k]):
                    drop.update(range(i - k, i + 1))
                    break
    kept = [l for i, l in enumerate(lines) if i not in drop]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip("\n")


def strip_signature(text: str) -> str:
    lines = text.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].rstrip() in ("--", "-- ", "—") and len(lines) - i <= 25:
            lines = lines[:i]
            break
    # mobile / client boilerplate
    lines = [l for l in lines if not re.match(r"^\s*(sent from my |get outlook for |sent from outlook|sent from mail for)", l, re.I)]
    # closing phrase followed by a short name/affiliation block to the end of the text
    while lines and not lines[-1].strip():
        lines.pop()
    for i in range(max(0, len(lines) - 6), len(lines)):
        if re.match(r"^\s*" + closers + r"\s*$", lines[i], re.I):
            tail = [x for x in lines[i + 1 :] if x.strip()]
            if len(tail) <= 4 and all(len(x) < 80 for x in tail):
                lines = lines[:i]
            break
    return "\n".join(lines).strip("\n")


def strip_footer(text: str) -> str:
    lines = text.split("\n")
    for i, l in enumerate(lines):
        if re.match(r"^\s*_{10,}\s*$", l) and any(
            re.search(RULES["mailman_footer_after"], x, re.I) for x in lines[i + 1 : i + 4]
        ):
            lines = lines[:i]
            break
    text = "\n".join(lines)
    paras = re.split(r"\n\s*\n", text)
    paras = [p for p in paras if not any(re.search(r, p, re.I | re.S) for r in RULES["boilerplate_paragraph"])]
    return "\n\n".join(paras).strip("\n")


def auto_reason(msg) -> str | None:
    a = str(msg.get("Auto-Submitted", "")).strip().lower()
    if a and a != "no":
        return "Auto-Submitted header"
    if msg.get("X-Auto-Response-Suppress") or str(msg.get("Precedence", "")).lower() in ("bulk", "junk"):
        return "bulk/auto-response header"
    frm = str(msg.get("From", ""))
    if re.search(RULES["auto_sender"], frm, re.I):
        return "bot sender"
    if re.search(RULES["auto_subject"], str(msg.get("Subject", "")), re.I):
        return "bot subject"
    return None


def tok(n: int) -> int:
    return round(n / 4)


def main() -> None:
    imap = load_imap_messages()
    rows, per_msg, texts = [], {}, {}
    drop_reasons: dict = {}
    for uid, (raw, msg) in imap.items():
        s1 = len(raw.decode("utf-8", "replace"))
        t2 = body_only(msg)
        t3 = strip_quotes(t2)
        t4 = strip_signature(t3)
        t5 = strip_footer(t4)
        reason = auto_reason(msg)
        if reason:
            drop_reasons[reason] = drop_reasons.get(reason, 0) + 1
        s6 = 0 if reason else len(t5)
        d = msg_date(msg)
        per_msg[uid] = {"date": d.date().isoformat() if d else None, "chars": [s1, len(t2), len(t3), len(t4), len(t5), s6], "dropped": reason is not None}
        texts[uid] = (t2, t5, reason)
    names = ["1 raw (with headers)", "2 body only", "3 quotes removed", "4 signature removed", "5 footer/Note Well removed", "6 auto mails dropped"]
    totals = [sum(v["chars"][i] for v in per_msg.values()) for i in range(6)]
    OUT.mkdir(exist_ok=True)
    with open(OUT / "preprocess.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["stage", "chars", "est_tokens", "pct_of_raw", "pct_of_body_only", "pct_of_previous_stage"])
        for i, n in enumerate(names):
            w.writerow([n, totals[i], tok(totals[i]), f"{100*totals[i]/totals[0]:.1f}", f"{100*totals[i]/totals[1]:.1f}" if i else "",
                        f"{100*totals[i]/totals[i-1]:.1f}" if i else ""])
    write_json(DATA / "preprocess_per_message.json", per_msg)

    n_empty_after = sum(1 for v in per_msg.values() if v["chars"][5] < 20 and not v["dropped"])
    lines = [
        "# Preprocessing reduction (sidrops, IMAP last 30 days) - numbers only",
        "",
        f"Messages: {len(imap)}; tokens are chars/4 estimates.",
        "",
        "| stage | chars | est. tokens | % of raw | % of body-only |",
        "|---|---:|---:|---:|---:|",
    ]
    for i, n in enumerate(names):
        lines.append(f"| {n} | {totals[i]} | {tok(totals[i])} | {100*totals[i]/totals[0]:.1f} | {'' if i == 0 else f'{100*totals[i]/totals[1]:.1f}'} |")
    lines += [
        "",
        f"- messages dropped as auto-generated: {sum(drop_reasons.values())} {drop_reasons}",
        f"- non-dropped messages with <20 chars left after stage 5: {n_empty_after}",
        f"- per-message token quartiles after stage 6 (kept messages): "
        + str(quartiles([tok(v['chars'][5]) for v in per_msg.values() if not v['dropped']])),
    ]
    (OUT / "preprocess.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))

    # 20 random before/after pairs for human review (gitignored)
    rnd = random.Random(0)
    cand = [u for u, (b, a, r) in texts.items() if not r and len(b.strip()) > 0]
    sdir = DATA / "samples"
    sdir.mkdir(parents=True, exist_ok=True)
    for old in sdir.glob("*"):
        old.unlink()
    for u in rnd.sample(cand, min(20, len(cand))):
        (sdir / f"{u}_before.txt").write_text(texts[u][0])
        (sdir / f"{u}_after.txt").write_text(texts[u][1])


def quartiles(xs):
    xs = sorted(xs)
    if not xs:
        return []
    q = lambda p: xs[min(len(xs) - 1, int(p * (len(xs) - 1)))]  # noqa: E731
    return {"min": xs[0], "q1": q(.25), "median": q(.5), "q3": q(.75), "max": xs[-1]}


if __name__ == "__main__":
    main()
