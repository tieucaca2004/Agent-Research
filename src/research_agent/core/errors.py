"""Typed errors. Every error carries a stable machine-readable ``code`` and a ``category``.

``code`` values are unchanged since Sprint 01 (backward compatible). ``category`` is the
normalized classification used for per-provider status reporting:

    CONFIGURATION_ERROR, AUTHENTICATION_ERROR, RATE_LIMITED, TIMEOUT, NETWORK_ERROR,
    PROVIDER_ERROR, INVALID_RESPONSE   (INTERNAL_ERROR / INVALID_INPUT for non-provider errors)

Error messages must never contain secrets (API keys, auth headers, keyed URLs).
"""

from __future__ import annotations

from collections.abc import Sequence


class ResearchAgentError(Exception):
    code: str = "INTERNAL_ERROR"
    category: str = "INTERNAL_ERROR"
    retryable: bool = False

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "category": self.category,
            "message": self.message,
            "retryable": self.retryable,
        }


class InvalidURLError(ResearchAgentError):
    code = "INVALID_URL"
    category = "INVALID_INPUT"


class ProviderConfigurationError(ResearchAgentError):
    """A provider cannot run because required configuration is missing."""

    code = "REQUIRES_CONFIGURATION"
    category = "CONFIGURATION_ERROR"

    def __init__(self, provider: str, missing: Sequence[str]) -> None:
        self.provider = provider
        self.missing = tuple(missing)
        super().__init__(f"{provider}: REQUIRES_CONFIGURATION (missing: {', '.join(self.missing)})")

    def to_dict(self) -> dict[str, object]:
        return {**super().to_dict(), "provider": self.provider, "missing": list(self.missing)}


class NoSearchProviderConfiguredError(ResearchAgentError):
    code = "REQUIRES_CONFIGURATION"
    category = "CONFIGURATION_ERROR"

    def __init__(self, details: Sequence[ProviderConfigurationError]) -> None:
        self.details = tuple(details)
        parts = "; ".join(d.message for d in self.details) or "SEARCH_PROVIDERS is empty"
        super().__init__(f"No search provider is configured: {parts}")


class ProviderError(ResearchAgentError):
    """Base for failures reported while calling an external provider."""

    code = "PROVIDER_ERROR"
    category = "PROVIDER_ERROR"

    def __init__(self, provider: str, message: str, *, status_code: int | None = None) -> None:
        self.provider = provider
        self.status_code = status_code
        super().__init__(f"{provider}: {message}")

    def to_dict(self) -> dict[str, object]:
        return {**super().to_dict(), "provider": self.provider, "status_code": self.status_code}


class ProviderUnavailableError(ProviderError):
    """Timeout, connection failure or 5xx. Safe to retry.

    Plain instances are server errors (5xx); timeouts and transport failures use the
    subclasses below so they can be told apart while keeping ``code`` unchanged.
    """

    code = "PROVIDER_UNAVAILABLE"
    retryable = True


class ProviderTimeoutError(ProviderUnavailableError):
    category = "TIMEOUT"


class ProviderNetworkError(ProviderUnavailableError):
    category = "NETWORK_ERROR"


class ProviderRateLimitedError(ProviderError):
    code = "PROVIDER_RATE_LIMITED"
    category = "RATE_LIMITED"
    retryable = True

    def __init__(self, provider: str, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(provider, message, status_code=429)
        self.retry_after_s = retry_after_s


class ProviderAuthError(ProviderError):
    """Credentials were rejected (401/403). Not retryable."""

    code = "PROVIDER_AUTH_FAILED"
    category = "AUTHENTICATION_ERROR"


class ProviderRequestError(ProviderError):
    """The provider rejected the request (other 4xx). Not retryable."""

    code = "PROVIDER_REQUEST_REJECTED"


class ProviderResponseError(ProviderError):
    """The provider answered with a body that does not match its documented contract."""

    code = "PROVIDER_MALFORMED_RESPONSE"
    category = "INVALID_RESPONSE"


class AllSearchProvidersFailedError(ResearchAgentError):
    code = "ALL_SEARCH_PROVIDERS_FAILED"

    def __init__(self, errors: Sequence[ProviderError]) -> None:
        self.errors = tuple(errors)
        summary = "; ".join(sorted({f"{e.provider}:{e.code}" for e in self.errors}))
        super().__init__(f"All search providers failed ({summary})")
