"""Search provider adapters. Application code imports from here, never from a vendor SDK."""

from research_agent.providers.search.base import SearchProvider
from research_agent.providers.search.registry import (
    PROVIDERS,
    ProviderStatus,
    build_search_providers,
    provider_statuses,
)

__all__ = [
    "PROVIDERS",
    "ProviderStatus",
    "SearchProvider",
    "build_search_providers",
    "provider_statuses",
]
