"""Shared text helpers."""

from __future__ import annotations

import re
from html.parser import HTMLParser


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
