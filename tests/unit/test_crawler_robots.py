"""Sprint 04: robots.txt matcher (RFC 9309 subset)."""

import pytest

from research_agent.crawler.robots import RobotsRules

UA = "ResearchAgentCrawler"


def allowed(robots: str, path: str, ua: str = UA) -> bool:
    return RobotsRules.parse(robots).is_allowed(ua, "https://example.com" + path)


@pytest.mark.parametrize(
    ("robots", "path", "expected"),
    [
        ("User-agent: *\nDisallow: /\n", "/", False),
        ("User-agent: *\nDisallow: /\n", "/anything", False),
        ("User-agent: *\nDisallow: /\nAllow: /public\n", "/public/page", True),
        ("User-agent: *\nDisallow: /\nAllow: /public\n", "/private", False),
        ("User-agent: *\nDisallow: /*.pdf$\n", "/menu.pdf", False),
        ("User-agent: *\nDisallow: /*.pdf$\n", "/menu.pdf?x=1", True),
        ("User-agent: *\nDisallow: /*.pdf$\n", "/menu.pdfx", True),
        ("User-agent: *\nDisallow: /*.pdf$\n", "/menu.html", True),
        ("User-agent: *\nDisallow: /private*/\n", "/private-x/a", False),
        ("User-agent: *\nDisallow: /a\nAllow: /a/b\n", "/a/b", True),  # longest match
        ("User-agent: *\nDisallow: /a\nAllow: /a/b\n", "/a/c", False),
        ("User-agent: *\nDisallow: /a$\nAllow: /a$\n", "/a", True),  # tie → allow
        ("User-agent: *\nDisallow:\n", "/x", True),  # empty disallow
        ("", "/x", True),
        ("User-agent: *\nDisallow: /search\n", "/search?q=x", False),
        ("User-agent: *\nDisallow: /search\n", "/searching", False),  # prefix match
        ("User-agent: *\nDisallow: /%7Euser\n", "/~user/x", False),  # percent-encoding equivalence
        ("User-agent: *\nDisallow: /x # comment\n", "/x", False),
        ("# only comments\n", "/x", True),
    ],
)
def test_rules(robots: str, path: str, expected: bool) -> None:
    assert allowed(robots, path) is expected


def test_specific_user_agent_group_wins_over_star() -> None:
    robots = "User-agent: *\nDisallow: /\n\nUser-agent: researchagentcrawler\nAllow: /\n"
    assert allowed(robots, "/x")
    assert not allowed(robots, "/x", ua="OtherBot")


def test_consecutive_user_agent_lines_share_a_group() -> None:
    robots = "User-agent: foo\nUser-agent: ResearchAgentCrawler\nDisallow: /secret\n"
    assert not allowed(robots, "/secret")
    assert allowed(robots, "/open")


def test_group_for_other_agent_does_not_apply() -> None:
    robots = "User-agent: Googlebot\nDisallow: /\n"
    assert allowed(robots, "/x")


def test_directives_are_case_insensitive() -> None:
    assert not allowed("USER-AGENT: *\nDISALLOW: /x\n", "/x")


def test_allow_all_factory() -> None:
    assert RobotsRules.allow_all().is_allowed(UA, "https://example.com/anything")
