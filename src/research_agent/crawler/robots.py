"""robots.txt parsing and matching (RFC 9309 subset).

``urllib.robotparser`` is not used: it ignores ``*``/``$`` and so allows paths that site owners
disallowed (Sprint 04 design, probe E12).

Supported: ``User-agent`` groups (case-insensitive product token, consecutive user-agent lines
share a group, the most specific matching group wins, ``*`` otherwise), ``Allow``/``Disallow``
with ``*`` and ``$``, longest match wins, ``Allow`` wins ties, empty ``Disallow`` = allow.
Other lines (``Sitemap``, ``Crawl-delay`` …) are ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit


def _normalize_path(path: str) -> str:
    # Compare in a canonical percent-encoded form (decode then re-encode, keeping reserved chars).
    return quote(unquote(path), safe="/?=&*$:@!,;+~%-._")


@dataclass(frozen=True)
class _Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(regex + ("$" if anchored else ""))


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[_Rule] = field(default_factory=list)


class RobotsRules:
    def __init__(self, groups: list[_Group]) -> None:
        self._groups = groups

    @classmethod
    def allow_all(cls) -> RobotsRules:
        return cls([])

    @classmethod
    def parse(cls, text: str) -> RobotsRules:
        groups: list[_Group] = []
        current: _Group | None = None
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if current is None or not last_was_agent:
                    current = _Group()
                    groups.append(current)
                current.agents.append(value.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if current is None or key not in ("allow", "disallow"):
                continue
            if key == "disallow" and value == "":
                continue  # empty Disallow = no restriction
            if not value:
                continue
            pattern = _normalize_path(value)
            current.rules.append(_Rule(key == "allow", pattern, _compile(pattern)))
        return cls(groups)

    def _rules_for(self, product_token: str) -> list[_Rule]:
        token = product_token.lower()
        specific = [g for g in self._groups if token in g.agents]
        chosen = specific or [g for g in self._groups if "*" in g.agents]
        return [rule for group in chosen for rule in group.rules]

    def is_allowed(self, product_token: str, url: str) -> bool:
        parts = urlsplit(url)
        path = _normalize_path((parts.path or "/") + (f"?{parts.query}" if parts.query else ""))
        best: _Rule | None = None
        for rule in self._rules_for(product_token):
            if rule.regex.match(path) is None:
                continue
            if (
                best is None
                or len(rule.pattern) > len(best.pattern)
                or (len(rule.pattern) == len(best.pattern) and rule.allow and not best.allow)
            ):
                best = rule
        return best is None or best.allow
