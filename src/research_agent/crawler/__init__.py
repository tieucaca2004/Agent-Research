"""Secure crawler / source fetching (Sprint 04). Internal subsystem — no HTTP endpoint."""

from research_agent.crawler.config import CrawlerSettings
from research_agent.crawler.crawler import ROBOTS_AGENT, USER_AGENT, Crawler, UnsafeTestOverrides
from research_agent.crawler.models import (
    CrawlContext,
    FetchError,
    FetchResult,
    FetchStatus,
    FetchTarget,
    RedirectHop,
    RobotsDecision,
)
from research_agent.crawler.network import CrawlerSetupError

__all__ = [
    "ROBOTS_AGENT",
    "USER_AGENT",
    "CrawlContext",
    "Crawler",
    "CrawlerSettings",
    "CrawlerSetupError",
    "FetchError",
    "FetchResult",
    "FetchStatus",
    "FetchTarget",
    "RedirectHop",
    "RobotsDecision",
    "UnsafeTestOverrides",
]
