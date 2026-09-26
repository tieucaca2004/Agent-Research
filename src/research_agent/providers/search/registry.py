"""Search provider registry: maps names in ``SEARCH_PROVIDERS`` to adapter classes."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from research_agent.config import Settings
from research_agent.core.errors import (
    NoSearchProviderConfiguredError,
    ProviderConfigurationError,
)
from research_agent.logging import get_logger
from research_agent.providers.search.base import SearchProvider
from research_agent.providers.search.google import GoogleSearchProvider
from research_agent.providers.search.perplexity import PerplexitySearchProvider

log = get_logger(__name__)

PROVIDERS: dict[str, type[SearchProvider]] = {
    PerplexitySearchProvider.name: PerplexitySearchProvider,
    GoogleSearchProvider.name: GoogleSearchProvider,
}


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    configured: bool
    missing: tuple[str, ...]

    @property
    def status(self) -> str:
        return "CONFIGURED" if self.configured else "REQUIRES_CONFIGURATION"


def provider_statuses(settings: Settings) -> list[ProviderStatus]:
    """Configuration status per requested provider. Never includes secret values."""
    statuses: list[ProviderStatus] = []
    for name in settings.search_providers:
        cls = PROVIDERS.get(name)
        if cls is None:
            statuses.append(ProviderStatus(name, False, ("UNKNOWN_PROVIDER",)))
            continue
        missing = tuple(cls.missing_settings(settings))
        statuses.append(ProviderStatus(name, not missing, missing))
    return statuses


def build_search_providers(settings: Settings, client: httpx.AsyncClient) -> list[SearchProvider]:
    """Instantiate configured providers in ``SEARCH_PROVIDERS`` order.

    Unconfigured providers are skipped with a warning; if none are usable,
    raises ``NoSearchProviderConfiguredError`` listing what is missing.
    """
    providers: list[SearchProvider] = []
    problems: list[ProviderConfigurationError] = []
    for name in settings.search_providers:
        cls = PROVIDERS.get(name)
        if cls is None:
            problems.append(ProviderConfigurationError(name, ["UNKNOWN_PROVIDER"]))
            continue
        try:
            providers.append(cls.from_settings(settings, client))
        except ProviderConfigurationError as exc:
            problems.append(exc)
    for problem in problems:
        log.warning(
            "search.provider_unavailable", provider=problem.provider, error=problem.to_dict()
        )
    if not providers:
        raise NoSearchProviderConfiguredError(problems)
    return providers
