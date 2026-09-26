import pytest

from research_agent.core.errors import InvalidURLError
from research_agent.core.urls import normalize_url, url_domain


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Example.COM", "https://example.com/"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("http://example.com:8080/a", "http://example.com:8080/a"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com/a/./b/../c", "https://example.com/a/c"),
        ("https://example.com/menu/", "https://example.com/menu/"),
        ("https://example.com/menu", "https://example.com/menu"),
        ("https://example.com//a//b", "https://example.com/a/b"),
        ("https://example.com/?b=2&a=1", "https://example.com/?a=1&b=2"),
        (
            "https://example.com/p?utm_source=x&UTM_Medium=y&id=5&fbclid=abc&gclid=1",
            "https://example.com/p?id=5",
        ),
        ("https://example.com/p?q=", "https://example.com/p?q="),
        ("https://user:pass@example.com/secret", "https://example.com/secret"),
        ("https://example.com./x", "https://example.com/x"),
        ("  https://example.com/x  ", "https://example.com/x"),
        ("https://nhàhàng.vn/menu", "https://xn--nhhng-sqab.vn/menu"),
        ("https://[2001:db8::1]:8443/x", "https://[2001:db8::1]:8443/x"),
        ("https://example.com/a%20b", "https://example.com/a%20b"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_normalize_is_idempotent() -> None:
    url = "HTTPS://Example.com:443/a/../b/?utm_source=x&z=1&a=2#f"
    once = normalize_url(url)
    assert normalize_url(once) == once


def test_relative_url_resolved_against_base() -> None:
    assert normalize_url("../menu?utm_campaign=x", base="https://example.com/a/b/") == (
        "https://example.com/a/menu"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "ftp://example.com/file",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "data:text/html,hi",
        "mailto:a@example.com",
        "https://",
        "https://example.com:99999/",
        "example.com/no-scheme",
        "https://example.com/" + "a" * 3000,
    ],
)
def test_invalid_urls_rejected(raw: str) -> None:
    with pytest.raises(InvalidURLError):
        normalize_url(raw)


def test_url_domain_strips_www() -> None:
    assert url_domain("https://www.example.com/x") == "example.com"
    assert url_domain("https://menu.example.com/x") == "menu.example.com"
