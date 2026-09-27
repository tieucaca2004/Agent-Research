"""Sprint 04: URL and address policy (pure functions)."""

import pytest

from research_agent.crawler.policy import (
    ALLOWED_PORTS,
    PolicyViolation,
    check_url,
    is_blocked_address,
)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.255.255.254",
        "10.1.2.3",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",  # noqa: S104 — address under test, not a bind
        "0.1.2.3",
        "224.0.0.1",
        "239.255.255.250",
        "255.255.255.255",
        "100.64.0.1",
        "240.0.0.1",
        "192.0.2.1",
        "198.18.0.1",
        "192.0.0.1",
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "fd12:3456::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
        "64:ff9b::7f00:1",
        "64:ff9b::a00:1",
        "64:ff9b:1::a9fe:a9fe",
        "2002:7f00:1::1",
        "2001::1",
        "2001:db8::1",
        "not-an-ip",
        "",
    ],
)
def test_blocked_addresses(address: str) -> None:
    assert is_blocked_address(address)


@pytest.mark.parametrize(
    "address", ["8.8.8.8", "1.1.1.1", "93.184.215.14", "2606:4700:4700::1111", "64:ff9b::808:808"]
)
def test_public_addresses_allowed(address: str) -> None:
    assert not is_blocked_address(address)


def test_is_global_alone_would_be_wrong() -> None:
    """Regression for probe E2: these report is_global=True in Python 3.11."""
    import ipaddress

    for tricky in ("224.0.0.1", "ff02::1", "64:ff9b::7f00:1"):
        assert ipaddress.ip_address(tricky).is_global
        assert is_blocked_address(tricky)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "http://example.com:80/",
        "https://example.com:443/path?q=1",
        "https://sub.example.com/a#frag",
        "http://nhàhàng.vn/menu",
        "http://8.8.8.8/",
    ],
)
def test_allowed_urls(url: str) -> None:
    assert check_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/f",
        "file:///etc/passwd",
        "gopher://example.com/",
        "data:text/html,hi",
        "javascript:alert(1)",
        "blob:https://example.com/uuid",
        "ws://example.com/",
        "http://example.com:8080/",
        "https://example.com:8443/",
        "http://example.com:22/",
        "http://user:pw@example.com/",
        "http://user@example.com/",
        "http:///nohost",
        "http://[fe80::1%25eth0]/",
        "http://example.com:99999/",
        "http://example.com/" + "a" * 3000,
    ],
)
def test_policy_blocked_urls(url: str) -> None:
    with pytest.raises(PolicyViolation) as info:
        check_url(url)
    assert not info.value.ssrf


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://LOCALHOST./",
        "http://foo.localhost/",
        "http://127.0.0.1/",
        "http://127.1/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://017700000001/",
        "http://0x7f.0.0.1/",
        "http://0/",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[64:ff9b::7f00:1]/",
        "http://[fc00::1]/",
        "http://metadata.google.internal/",
    ],
)
def test_ssrf_blocked_urls(url: str) -> None:
    with pytest.raises(PolicyViolation) as info:
        check_url(url)
    assert info.value.ssrf


def test_port_policy() -> None:
    assert frozenset({80, 443}) == ALLOWED_PORTS
    assert check_url("http://example.com:80") == "example.com"
    assert check_url("https://example.com:443") == "example.com"
    with pytest.raises(PolicyViolation, match="port"):
        check_url("http://example.com:8080")
    assert check_url("http://example.com:8080", extra_ports=frozenset({8080}))
