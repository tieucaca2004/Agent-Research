"""Run the local/internal Research API: ``uv run python -m research_agent.api``.

Binds to ``API_HOST`` (default 127.0.0.1). Do not expose publicly: no authentication and no
rate limiting yet (deferred).
"""

from __future__ import annotations

import uvicorn

from research_agent.api.app import create_app
from research_agent.api.config import ApiSettings
from research_agent.config import Settings
from research_agent.logging import configure_logging


def main() -> None:
    settings = ApiSettings()
    search_settings = Settings()
    configure_logging(search_settings.log_level, search_settings.log_format)
    uvicorn.run(
        create_app(settings, search_settings=search_settings),
        host=settings.api_host,
        port=settings.api_port,
        access_log=False,  # http.request structured log replaces the access log
        server_header=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
