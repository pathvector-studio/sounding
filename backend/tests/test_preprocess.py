"""Preprocessing of message bodies for the LLM (phase 2, R3). Synthetic corpus only."""

import time
from pathlib import Path

import pytest

from sounding.preprocess import PreprocessResult, preprocess_body

CORPUS = Path(__file__).parent / "corpus" / "preprocess"
CASES = sorted(p.name.removesuffix(".in.txt") for p in CORPUS.glob("*.in.txt"))


def run(text):
    return preprocess_body(text)


def test_corpus_is_not_empty():
    assert len(CASES) >= 15
    for name in CASES:
        assert (CORPUS / f"{name}.out.txt").exists(), name


@pytest.mark.parametrize("name", CASES)
def test_corpus(name):
    src = (CORPUS / f"{name}.in.txt").read_text()
    expected = (CORPUS / f"{name}.out.txt").read_text().rstrip("\n")
    result = run(src)
    assert result.text == expected
    assert result.chars_before == len(src)
    assert result.chars_after == len(result.text)
    assert not result.failed
    # idempotent: preprocessing already-preprocessed text changes nothing
    assert run(result.text).text == result.text


def test_top_post_has_no_context_and_counts_one_block():
    r = run((CORPUS / "top_post.in.txt").read_text())
    assert "[quoted]" not in r.text
    assert r.quote_blocks_removed == 1
    assert r.context_snippets == 0
    assert not r.signature_removed and not r.footer_removed


def test_interleaved_keeps_short_context_for_each_reply():
    r = run((CORPUS / "interleaved.in.txt").read_text())
    assert r.context_snippets == 3
    assert r.text.count("[quoted]") == 3
    assert r.quote_blocks_removed == 3


def test_context_is_truncated_and_limited_to_last_lines():
    quoted = "\n".join(f"> line {i} " + "x" * 150 for i in range(5))
    r = run(f"{quoted}\n\nmy answer")
    ctx = r.text.splitlines()[0]
    assert ctx.startswith("[quoted] ")
    assert len(ctx) <= len("[quoted] ") + 200
    assert "line 4" in ctx
    assert "line 0" not in ctx and "line 1" not in ctx and "line 2" not in ctx
    assert r.text.endswith("my answer")


def test_text_after_a_quote_is_never_dropped():
    r = run("> a\nkept one\n> b\nkept two\n\n> c\n\nkept three")
    for word in ("kept one", "kept two", "kept three"):
        assert word in r.text
    assert "\n> " not in r.text


def test_reply_below_outlook_block_survives():
    r = run((CORPUS / "reply_below_outlook.in.txt").read_text())
    assert "I support adoption" in r.text
    assert "appendix" in r.text
    assert "Sent:" not in r.text and "Subject:" not in r.text


def test_outlook_header_without_quotes_is_a_tail():
    r = run("Top reply.\n\nFrom: A <a@example.com>\nSent: today\nTo: b@example.com\nSubject: x\n\nold text\nmore old text")
    assert r.text == "Top reply."
    assert r.quote_blocks_removed == 1


def test_header_like_lines_without_enough_labels_are_text():
    src = "From: the draft says X\nSee also the Subject: line of the thread.\nTo: be decided."
    assert run(src).text == src


def test_attribution_like_sentence_is_kept():
    src = "On the other hand\nhe wrote: nothing useful about it"
    assert run(src).text == src
    src2 = "On balance, I think the author wrote:"
    assert run(src2).text == src2


def test_signature_flag_and_bare_dashes_far_from_end_are_text():
    r = run("Done.\n-- \nAlice\nExample Networks")
    assert r.text == "Done." and r.signature_removed
    long_tail = "\n".join(f"line {i}" for i in range(40))
    src = f"before\n--\n{long_tail}"
    assert run(src).text == src  # a bare "--" with a lot of text after it is not treated as a signature


def test_signature_does_not_swallow_text_after_a_following_quote():
    r = run("Reply one.\n-- \nSig\n\nOn 2 Nov 2026, Bob <bob@example.com> wrote:\n> q\n\nReply two.")
    assert "Sig" not in r.text
    assert "Reply one." in r.text and "Reply two." in r.text
    assert r.signature_removed


def test_footer_flags():
    r = run((CORPUS / "ietf_footer.in.txt").read_text())
    assert r.footer_removed and not r.signature_removed
    assert "mailing list" not in r.text


def test_underscore_rule_without_footer_text_is_kept():
    src = "Header\n________________________________\nSomething else entirely"
    assert run(src).text == src


def test_note_well_mention_in_normal_text_is_kept():
    src = "Please read the Note Well before the meeting; it is relevant to this thread."
    # a normal sentence is not a Note Well paragraph (no BCP / reminder wording)
    assert run(src).text == src


@pytest.mark.parametrize("value", [None, "", "   \n\n  ", "\x00\x00", b"\xff\xfe\x00binary", 12345])
def test_never_raises_on_odd_input(value):
    r = run(value)
    assert isinstance(r, PreprocessResult)
    assert isinstance(r.text, str)


def test_empty_input():
    r = run("")
    assert r == PreprocessResult(text="", chars_before=0, chars_after=0)
    assert run(None).text == ""


def test_only_quotes_gives_empty_text():
    r = run("On 2 Nov 2026, Bob <bob@example.com> wrote:\n> everything\n> is quoted\n")
    assert r.text == ""
    assert r.quote_blocks_removed == 1


@pytest.mark.parametrize(
    "line",
    [
        "a" * 2_000_000,
        ">" * 500_000 + " x",
        " " * 1_000_000 + "x",
        "On " + "wrote: " * 100_000,
        "From: " + "Sent: To: Subject: " * 50_000,
        "_" * 500_000,
        "-" * 500_000,
        "a:" * 300_000,
        "Note Well " * 20_000,
    ],
)
def test_pathological_single_line_is_fast(line):
    start = time.perf_counter()
    r = run(line)
    assert time.perf_counter() - start < 2.0
    assert isinstance(r.text, str)


def test_pathological_many_lines_is_fast():
    many_headers = "From: x\n" * 100_000
    many_attr = "On 1 Jan 2026,\n" * 100_000
    many_sig = "-- \nsig\n" * 50_000
    many_quotes = "> q\ntext\n" * 100_000
    for text in (many_headers, many_attr, many_sig, many_quotes):
        start = time.perf_counter()
        r = run(text)
        assert time.perf_counter() - start < 5.0
        assert isinstance(r.text, str)


def test_huge_input_is_truncated_not_rejected():
    r = run("word " * 1_000_000)
    assert r.truncated_input
    assert r.chars_before == 5_000_000
    assert r.chars_after > 0
