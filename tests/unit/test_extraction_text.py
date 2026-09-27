"""Sprint 05: text normalization (design section 11, probe P9)."""

import unicodedata

import pytest

from research_agent.extraction.text import (
    clean_inline,
    clean_plain_text,
    clean_pre,
    clean_value,
)

VI = "Phở bò, cá hồi, Nguyễn Trãi, đặc biệt"


def test_nfd_vietnamese_becomes_nfc() -> None:
    nfd = unicodedata.normalize("NFD", VI)
    assert nfd != VI
    assert clean_inline(nfd) == VI
    assert clean_pre(nfd) == VI
    assert clean_plain_text(nfd) == VI


@pytest.mark.parametrize(
    "value",
    ["E=mc²", "① ② ③", "価格：１２３円", "ｶﾀｶﾅ", "½ cup", "Python™", "ﬁle"],  # noqa: RUF001
)
def test_nfkc_is_not_applied(value: str) -> None:
    assert unicodedata.normalize("NFKC", value) != value  # these would change under NFKC
    assert clean_inline(value) == value
    assert clean_plain_text(value) == value


def test_zwj_and_zwnj_are_kept() -> None:
    family = "👨\u200d👩\u200d👧"
    persian = "می\u200cخواهم"  # noqa: RUF001
    assert clean_inline(f"{family} {persian}") == f"{family} {persian}"
    assert clean_pre(family) == family
    assert clean_plain_text(persian) == persian


@pytest.mark.parametrize("char", ["\u200b", "\u2060", "\ufeff", "\u00ad"])
def test_invisible_characters_removed(char: str) -> None:
    assert clean_inline(f"ab{char}cd") == "abcd"
    assert clean_pre(f"ab{char}cd") == "abcd"
    assert clean_plain_text(f"ab{char}cd") == "abcd"


def test_control_characters_removed_but_newline_and_tab_kept_where_meaningful() -> None:
    raw = "a\x00b\x07c\x1bd\x7fe\x85f\x9fg"
    assert clean_inline(raw) == "abcdefg"
    assert clean_pre("x\ty\nz\x00") == "x\ty\nz"
    assert clean_plain_text("x\ty\nz\x0b") == "x\ty\nz"


def test_nbsp_variants_become_spaces_outside_pre() -> None:
    assert clean_inline("100\u00a0000\u202f₫\u2007x") == "100 000 ₫ x"
    assert clean_plain_text("100\u00a0000") == "100 000"
    assert clean_pre("a\u00a0b") == "a\u00a0b"


def test_html_whitespace_collapsed_inline() -> None:
    assert clean_inline("  a \t\n\r\f  b\u00a0\u00a0 c  ") == "a b c"


def test_pre_is_verbatim_except_line_endings() -> None:
    code = "def f():\r\n    return  1\r\n\r\n\tx = '  '\r"
    assert clean_pre(code) == "def f():\n    return  1\n\n\tx = '  '\n"


def test_plain_text_keeps_lines_and_reduces_long_blank_runs() -> None:
    raw = "line 1  \r\nline 2\n\n\n\n\n\nline 3\n\n\nline 4\n"
    assert clean_plain_text(raw) == "line 1\nline 2\n\n\nline 3\n\n\nline 4"


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/a?b=1&c=%E1%BB%9F#frag",
        "120.000đ",
        "1.250.000 ₫",
        "2026-09-27T12:00:00+07:00",
        "08:00–22:00",  # noqa: RUF001
        "+84 28 1234 5678",
        "if (a <= b) { return x; }",
        "中文字符 日本語のテキスト 😀🎉",
        "[1] Smith et al., 2020, p. 42",
    ],
)
def test_values_are_not_damaged(value: str) -> None:
    assert clean_inline(value) == value
    assert clean_plain_text(value) == value
    assert clean_pre(value) == value


def test_clean_value_limits_and_empty() -> None:
    assert clean_value("   \n ", 10) == (None, False)
    assert clean_value("  a  b ", 10) == ("a b", False)
    assert clean_value("x" * 11, 10) == ("x" * 10, True)
    assert clean_value("x" * 10, 10) == ("x" * 10, False)
