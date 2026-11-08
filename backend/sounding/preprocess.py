"""Preprocess one message's stored body text into the text handed to the LLM (spec phase 2, R3).

Input is ``items.body_text`` (already plain text, not raw MIME). The function is pure, line based
(no regex is ever run on a line longer than ``MAX_MATCH_LINE`` characters, and every pattern is
anchored or bounded, so there is no catastrophic backtracking) and never raises.

What is removed
  * quoted text: ``>`` prefixed lines at any depth, "On ... wrote:" attributions (also wrapped over
    up to three lines, and localized), Outlook/Gmail-style header blocks and
    "-----Original Message-----" markers (also localized);
  * the RFC 3676 signature (``-- `` up to the next quote region) and mobile client footers;
  * mailing list footers and boilerplate paragraphs (``IETF_RULES``, ``GENERIC_RULES``).

What is kept
  * every unquoted, non-header line that is not a signature/footer, including text that follows a quote;
  * for text that follows a quote block (interleaved or bottom-posted replies) a short
    ``[quoted] ...`` line made of the last one or two quoted lines. Top-posting keeps none.

Header blocks: an Outlook-style header (or a marker line) starts a "forwarded region". If no ``>``
line follows it, everything after it is unprefixed forwarded/quoted material and is dropped as the
tail of the message. If ``>`` lines follow, the unprefixed lines are treated as the author's text
(a reply written below the block) and only the header and the ``>`` lines are dropped. Known limit:
an unprefixed original followed by an unprefixed reply cannot be told apart and is dropped as a tail.

Auto-generated mails are not handled here: they are classified earlier and passed as events.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Inputs larger than this are truncated before processing (a mail body is never this big in practice).
MAX_INPUT_CHARS = 2_000_000
# Lines longer than this are never matched against the attribution/header/marker patterns.
MAX_MATCH_LINE = 1000
CONTEXT_MAX_CHARS = 200
CONTEXT_MAX_LINES = 2
QUOTED_MARK = "[quoted] "

# --- Knowledge that is specific to one mailing list system (kept apart from the generic rules) ---
IETF_RULES = {
    # A line made only of these characters, followed within FOOTER_LOOKAHEAD lines by a footer line,
    # starts a mailman footer; the footer extends to the next blank line (at most FOOTER_MAX_LINES).
    "footer_separator": re.compile(r"^_{10,}$"),
    "footer_lines": [
        re.compile(r"\bmailing list\b", re.I),
        re.compile(r"mailman/listinfo", re.I),
        re.compile(r"\bto unsubscribe send\b", re.I),
        re.compile(r"-leave@", re.I),
        re.compile(r"\bunsubscribe\b.{0,80}@", re.I),
    ],
    # Paragraphs (blank-line separated, at most BOILERPLATE_MAX_CHARS long) matching any of these are dropped.
    "boilerplate_paragraphs": [
        re.compile(r"this is a reminder of ietf policies in effect", re.I),
        re.compile(r"the ietf'?s patent policy and the definition of an ietf", re.I),
        re.compile(r"\A\W*note well\b(?=.{0,2000}(?:bcp|ietf policies|reminder|contribution))", re.I | re.S),
    ],
}
BARE_SIG_MAX_LINES = 25
FOOTER_LOOKAHEAD = 4
FOOTER_MAX_LINES = 8
BOILERPLATE_MAX_CHARS = 2500

# Not list specific: client footers and legal disclaimers.
GENERIC_RULES = {
    "client_footer_lines": [
        re.compile(
            r"^(?:sent from my [\w .\-]{1,40}|sent from (?:outlook|mail|yahoo mail|proton ?mail|gmail|samsung[\w ]{0,30}|"
            r"my [\w .\-]{1,40})|get outlook for [\w ]{1,20}|download outlook for [\w ]{1,20}|"
            r"sent (?:via|with) [\w .\-]{1,40}|von meinem [\w .\-]{1,30} gesendet|envoy[ée] de mon [\w .\-]{1,30}|"
            r"enviado desde mi [\w .\-]{1,30}|从我的[\w ]{1,20}发送|從我的[\w ]{1,20}發送|"
            r"iphone ?から送信|[\w ]{1,20}から送信)[ .]*$",
            re.I,
        ),
    ],
    "boilerplate_paragraphs_all_of": [
        (re.compile(r"intended recipient", re.I), re.compile(r"confidential|privileged", re.I)),
    ],
}

# --- Quote / forwarded-region detection tables ---
_ATTR_VERB = re.compile(
    r"\b(?:wrote|writes|said|schrieb|a écrit|a ecrit|escribió|escribio|scrisse|schreef|skrev)\b|写道|寫道|書きました|书道",
    re.I,
)
_ATTR_STARTERS = ("on ", "am ", "le ", "el ", "op ", "il ", "den ", "på ", "in data ")

_MARKER = re.compile(
    r"^[\-–—_=* ]*(?:original message|forwarded message|begin forwarded message|original-nachricht|"
    r"ursprüngliche nachricht|weitergeleitete nachricht|message d.origine|message transféré|mensaje original|"
    r"原始邮件|原始郵件|转发的邮件|轉寄的郵件|元のメッセージ|転送メッセージ|転送されたメッセージ)[\-–—_=* :]*$",
    re.I,
)
_RULE_LINE = re.compile(r"^(?:_{10,}|-{10,})$")

_FROM, _DATE, _TO, _CC, _SUBJECT = "from", "date", "to", "cc", "subject"
_LABELS: dict[str, str] = {}
for _cat, _names in {
    _FROM: "from 发件人 發件人 寄件者 寄件人 差出人 送信者 送信元 von de da van från",
    _DATE: "sent date 发送时间 發送時間 发送日期 日期 寄件日期 送信日時 送信日 日時 日付 gesendet datum envoyé envoye enviado inviato skickat verzonden",
    _TO: "to 收件人 收件者 宛先 宛名 an à a para aan till",
    _CC: "cc 抄送 副本 コピー",
    _SUBJECT: "subject 主题 主題 主旨 件名 betreff objet asunto oggetto onderwerp ämne",
}.items():
    for _n in _names.split():
        _LABELS[_n] = _cat
_SINGLE_LINE_HDR = re.compile(r"\b(?:sent|date|to|subject)\s*:", re.I)
_HEADER_BLOCK_MAX_LINES = 14


@dataclass(frozen=True)
class PreprocessResult:
    text: str
    chars_before: int
    chars_after: int
    quote_blocks_removed: int = 0
    signature_removed: bool = False
    footer_removed: bool = False
    context_snippets: int = 0  # number of "[quoted] ..." lines kept
    truncated_input: bool = False
    failed: bool = False  # an unexpected error happened; text is the whitespace-normalised input


# ---------------------------------------------------------------------------------------------
# line classifiers


def _is_quote(line: str) -> bool:
    return line.lstrip(" \t").startswith(">")


def _quote_content(line: str) -> str:
    s = line.lstrip(" \t")
    i = 0
    while i < len(s) and s[i] in "> \t":
        i += 1
    return s[i:].strip()


def _next_nonblank_is_quote(lines: list[str], i: int) -> bool:
    for j in range(i, min(len(lines), i + 4)):
        if lines[j].strip():
            return _is_quote(lines[j])
    return False


def _attr_line_ok(s: str) -> bool:
    if not s or len(s) > 400 or s[-1] not in ":：":
        return False
    m = _ATTR_VERB.search(s)
    if m and len(s) - m.end() <= 120:
        return True
    # Japanese Gmail style: "2026年11月1日(日) 10:00 Alice <alice@example.com>:"
    return "年" in s and "@" in s and len(s) > 1 and s[-2] == ">"


def _match_attribution(lines: list[str], i: int) -> int:
    """Return the end index (exclusive) of an attribution starting at ``i``, or 0."""
    n = len(lines)
    parts: list[str] = []
    for k in range(3):
        if i + k >= n:
            break
        s = lines[i + k].strip()
        if len(s) > 400 or not s or (k > 0 and _is_quote(s)):
            break
        parts.append(s)
        if k > 0 and not parts[0].lower().startswith(_ATTR_STARTERS):
            break
        joined = " ".join(parts)
        if _attr_line_ok(joined) and (
            any(c.isdigit() or c in "@<" for c in joined) or _next_nonblank_is_quote(lines, i + k + 1)
        ):
            return i + k + 1
    return 0


def _is_marker(line: str) -> bool:
    s = line.strip()
    return 0 < len(s) <= 100 and _MARKER.match(s) is not None


def _label_category(line: str) -> str | None:
    s = line.strip().lstrip("*_ ")
    if not s or len(s) > MAX_MATCH_LINE:
        return None
    pos = -1
    for idx, ch in enumerate(s[:24]):
        if ch in ":：":
            pos = idx
            break
    if pos <= 0:
        return None
    return _LABELS.get(s[:pos].strip().rstrip("*_ ").lower())


def _match_outlook_block(lines: list[str], i: int) -> int:
    """Return the end index (exclusive) of a header block starting at ``i`` ("From: ... Sent: ..."), or 0."""
    if i >= len(lines) or _label_category(lines[i]) != _FROM:
        return 0
    first = lines[i]
    if len(first) <= MAX_MATCH_LINE:
        hits = {m.group(0).split(":")[0].strip().lower() for m in _SINGLE_LINE_HDR.finditer(first)}
        if len(hits) >= 2 and "subject" in hits:
            return i + 1
    cats: set[str] = set()
    j = i + 1
    while j < len(lines) and j - i <= _HEADER_BLOCK_MAX_LINES:
        s = lines[j]
        if not s.strip() or _is_quote(s):
            break
        cat = _label_category(s)
        if cat:
            cats.add(cat)
        elif not (s[0] in " \t" or "@" in s or ";" in s):
            break
        j += 1
    cats.discard(_FROM)
    if len(cats) >= 2 and cats & {_DATE, _SUBJECT}:
        return j
    return 0


def _match_forward_region(lines: list[str], i: int) -> int:
    """A marker line, a rule line directly above a header block, or a header block. Returns end index or 0."""
    line = lines[i]
    if len(line) > MAX_MATCH_LINE:
        return 0
    if _is_marker(line):
        return i + 1
    if _RULE_LINE.match(line.strip()):
        end = _match_outlook_block(lines, i + 1)
        return end if end else 0
    return _match_outlook_block(lines, i)


def _starts_quote_region(lines: list[str], i: int) -> bool:
    return _is_quote(lines[i]) or _match_attribution(lines, i) > 0 or _match_forward_region(lines, i) > 0


def _is_sig_delimiter(line: str) -> bool:
    return line in ("-- ", "--")


def _match_footer(lines: list[str], i: int) -> int:
    """Mailman-style list footer starting at ``i``. Returns the end index (exclusive) or 0."""
    s = lines[i].strip()
    if len(s) > MAX_MATCH_LINE or not IETF_RULES["footer_separator"].match(s):
        return 0
    window = lines[i + 1 : i + 1 + FOOTER_LOOKAHEAD]
    if not any(len(x) <= MAX_MATCH_LINE and any(p.search(x) for p in IETF_RULES["footer_lines"]) for x in window):
        return 0
    j = i + 1
    while j < len(lines) and lines[j].strip() and j - i <= FOOTER_MAX_LINES:
        j += 1
    return j


def _is_client_footer(line: str) -> bool:
    s = line.strip()
    return 0 < len(s) <= 120 and any(p.match(s) for p in GENERIC_RULES["client_footer_lines"])


def _is_boilerplate_paragraph(par: str) -> bool:
    if len(par) > BOILERPLATE_MAX_CHARS:
        return False
    if any(p.search(par) for p in IETF_RULES["boilerplate_paragraphs"]):
        return True
    return any(all(p.search(par) for p in pats) for pats in GENERIC_RULES["boilerplate_paragraphs_all_of"])


# ---------------------------------------------------------------------------------------------
# main


@dataclass
class _Seg:
    quote: bool
    lines: list[str]  # quote segments: original Q lines, "" for dropped header lines
    has_quote_lines: bool = False


def _process(text: str) -> PreprocessResult:
    chars_before = len(text)
    truncated = False
    if len(text) > MAX_INPUT_CHARS:
        text = text[:MAX_INPUT_CHARS]
        truncated = True
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    n = len(lines)

    last_q = -1
    for idx in range(n - 1, -1, -1):
        if _is_quote(lines[idx]):
            last_q = idx
            break

    signature_removed = False
    footer_removed = False
    # kinds: T text, B blank, Q quoted, H dropped header/marker/attribution, D dropped silently
    kinds: list[tuple[str, str]] = []
    i = 0
    while i < n:
        line = lines[i]
        if not line.strip():
            kinds.append(("B", ""))
            i += 1
            continue
        if _is_quote(line):
            kinds.append(("Q", line))
            i += 1
            continue
        end = _match_attribution(lines, i)
        if end:
            kinds.extend(("H", x) for x in lines[i:end])
            i = end
            continue
        end = _match_forward_region(lines, i)
        if end:
            kinds.extend(("H", x) for x in lines[i:end])
            i = end
            if last_q < end:  # nothing quoted after it: the rest is the forwarded / quoted tail
                kinds.extend(("H", x) for x in lines[i:])
                i = n
            continue
        if _is_sig_delimiter(line):
            j = i + 1
            while j < n and not _starts_quote_region(lines, j):
                j += 1
            if line == "-- " or j - i <= BARE_SIG_MAX_LINES:  # a bare "--" far from the end may be a rule
                kinds.extend(("D", "") for _ in range(i, j))
                signature_removed = True
                i = j
                continue
        end = _match_footer(lines, i)
        if end:
            kinds.extend(("D", "") for _ in range(i, end))
            footer_removed = True
            i = end
            continue
        if _is_client_footer(line):
            kinds.append(("D", ""))
            signature_removed = True
            i += 1
            continue
        kinds.append(("T", line))
        i += 1

    # group into alternating text / quote segments
    segs: list[_Seg] = []
    cur: _Seg | None = None
    for kind, line in kinds:
        if kind == "D":
            continue
        if kind == "T":
            if cur is None or cur.quote:
                cur = _Seg(False, [])
                segs.append(cur)
            cur.lines.append(line)
        elif kind == "B":
            if cur is not None:
                cur.lines.append("")
        else:  # Q or H
            if cur is None or not cur.quote:
                cur = _Seg(True, [])
                segs.append(cur)
            cur.lines.append(line if kind == "Q" else "")
            if kind == "Q":
                cur.has_quote_lines = True
    quote_blocks = sum(1 for s in segs if s.quote)

    pieces: list[str] = []
    snippets = 0
    for idx, seg in enumerate(segs):
        if seg.quote:
            continue
        body = _drop_boilerplate("\n".join(seg.lines).strip("\n"))
        if body != "\n".join(seg.lines).strip("\n"):
            footer_removed = True
        if not body.strip():
            continue
        if idx > 0 and segs[idx - 1].quote:
            ctx = _context(segs[idx - 1])
            if ctx:
                body = ctx + "\n" + body
                snippets += 1
        pieces.append(body)

    out = _normalise_whitespace("\n\n".join(pieces))
    return PreprocessResult(
        text=out,
        chars_before=chars_before,
        chars_after=len(out),
        quote_blocks_removed=quote_blocks,
        signature_removed=signature_removed,
        footer_removed=footer_removed,
        context_snippets=snippets,
        truncated_input=truncated,
    )


def _context(quote_seg: _Seg) -> str:
    contents: list[str] = []
    for line in reversed(quote_seg.lines):
        if not line:
            continue
        c = _quote_content(line)
        if c and any(ch.isalnum() for ch in c[:200]):
            contents.append(c[:CONTEXT_MAX_CHARS * 2])
            if len(contents) >= CONTEXT_MAX_LINES:
                break
    if not contents:
        return ""
    joined = " ".join(reversed(contents))
    if len(joined) > CONTEXT_MAX_CHARS:
        joined = "…" + joined[-(CONTEXT_MAX_CHARS - 1) :]
    return QUOTED_MARK + joined


def _drop_boilerplate(body: str) -> str:
    if "\n\n" not in body and not _is_boilerplate_paragraph(body):
        return body
    paras = re.split(r"\n[ \t]*\n", body)
    kept = [p for p in paras if not _is_boilerplate_paragraph(p)]
    return "\n\n".join(kept)


def _normalise_whitespace(text: str) -> str:
    out: list[str] = []
    blank = 0
    for line in text.split("\n"):
        line = line.rstrip()
        if not line:
            blank += 1
            if blank == 1:
                out.append("")
            continue
        blank = 0
        out.append(line)
    return "\n".join(out).strip("\n")


def _fallback(text: str, chars_before: int) -> PreprocessResult:
    out = _normalise_whitespace(text[:MAX_INPUT_CHARS].replace("\r\n", "\n").replace("\r", "\n"))
    return PreprocessResult(text=out, chars_before=chars_before, chars_after=len(out), failed=True)


def preprocess_body(body_text: str | bytes | None) -> PreprocessResult:
    """Turn one message's ``body_text`` into LLM input. Pure; never raises."""
    if body_text is None:
        return PreprocessResult(text="", chars_before=0, chars_after=0)
    try:
        if isinstance(body_text, bytes | bytearray):
            body_text = bytes(body_text).decode("utf-8", "replace")
        elif not isinstance(body_text, str):
            body_text = str(body_text)
    except Exception:  # noqa: BLE001
        return PreprocessResult(text="", chars_before=0, chars_after=0, failed=True)
    try:
        return _process(body_text)
    except Exception:  # noqa: BLE001 - never let a odd input stop the pipeline
        try:
            return _fallback(body_text, len(body_text))
        except Exception:  # noqa: BLE001
            return PreprocessResult(text="", chars_before=len(body_text), chars_after=0, failed=True)
