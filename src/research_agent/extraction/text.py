"""Text normalization (Sprint 05 design §11).

NFC only (NFKC changes meaning, e.g. ``mc²`` → ``mc2``). Removed: C0/C1 controls except ``\\n``
and ``\\t``, ZWSP, word joiner, BOM, soft hyphen. Kept: ZWJ / ZWNJ (emoji sequences, Persian),
bidi marks, combining marks. NBSP-like spaces become ordinary spaces outside ``<pre>``.
"""

from __future__ import annotations

import re
import unicodedata

_REMOVED = {cp: None for cp in (*range(0x00, 0x09), 0x0B, 0x0C, 0x0D, *range(0x0E, 0x20))}
_REMOVED.update({cp: None for cp in range(0x7F, 0xA0)})
_REMOVED.update({0x200B: None, 0x2060: None, 0xFEFF: None, 0x00AD: None})

_SPACES = {0x00A0: " ", 0x202F: " ", 0x2007: " "}
# Inline: HTML whitespace (tab, LF, FF, CR) becomes a space and is then collapsed.
_INLINE_TABLE: dict[int, str | None] = {
    **_REMOVED,
    **_SPACES,
    **{cp: " " for cp in (0x09, 0x0A, 0x0C, 0x0D)},
}
# Plain text keeps newlines and tabs; line endings are normalized before the table.
_PLAIN_TABLE: dict[int, str | None] = {**_REMOVED, **_SPACES}
_PRE_TABLE: dict[int, str | None] = dict(_REMOVED)

_SPACE_RUN = re.compile(r" {2,}")
_MANY_NEWLINES = re.compile(r"\n{3,}")
_MANY_BLANK_LINES = re.compile(r"\n{4,}")
_TRAILING_SPACE = re.compile(r"[ \t]+\n")


def _line_endings(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def clean_inline(value: str) -> str:
    """Inline text: HTML whitespace collapsed to one space, NFC, invisible chars removed."""
    value = _SPACE_RUN.sub(" ", value.translate(_INLINE_TABLE))
    return unicodedata.normalize("NFC", value).strip()


def clean_pre(value: str) -> str:
    """Preformatted text: kept verbatim except line endings, invisible chars and NFC."""
    return unicodedata.normalize("NFC", _line_endings(value).translate(_PRE_TABLE))


def clean_block(value: str) -> str:
    """A rendered non-pre block: trailing spaces removed, at most one blank line in a row."""
    return _MANY_NEWLINES.sub("\n\n", _TRAILING_SPACE.sub("\n", value)).strip("\n")


def clean_plain_text(value: str) -> str:
    """``text/plain``: line structure kept, runs of 3+ blank lines reduced to 2."""
    value = _line_endings(value).translate(_PLAIN_TABLE)
    value = _TRAILING_SPACE.sub("\n", unicodedata.normalize("NFC", value))
    return _MANY_BLANK_LINES.sub("\n\n\n", value).strip("\n")


def clean_value(value: str, limit: int) -> tuple[str | None, bool]:
    """Title/metadata value, inline-cleaned and cut to ``limit`` chars: (value, truncated)."""
    cleaned = clean_inline(value)
    if not cleaned:
        return None, False
    if len(cleaned) > limit:
        return cleaned[:limit].rstrip(), True
    return cleaned, False
