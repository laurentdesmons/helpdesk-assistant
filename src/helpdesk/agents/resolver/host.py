"""Container entrypoint for the Foundry-hosted resolver.

Thin: no business logic. Builds the same MAF ``Agent`` the local path uses and
serves it over the Responses protocol (``POST /responses`` + ``GET /readiness``,
port ``PORT`` env or 8088).

    python -m helpdesk.agents.resolver.host      # local host on :8088

The Foundry runtime runs this via ``main.py`` (``HELPDESK_AGENT_ROLE=resolver``)
and injects ``FOUNDRY_PROJECT_ENDPOINT`` (+ ``APPLICATIONINSIGHTS_CONNECTION_
STRING`` once App Insights is wired in Phase 4). ``HELPDESK_SEARCH_ENDPOINT`` and
the embedding / index settings come through ``azure.yaml`` ``env:``.
"""

from __future__ import annotations

from agent_framework_foundry_hosting import ResponsesHostServer

from helpdesk.agents.resolver.agent import build_resolver_agent
from helpdesk.config import get_settings
from helpdesk.logging import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    agent = build_resolver_agent(settings)  # pure constructor — no model call
    ResponsesHostServer(agent).run()


if __name__ == "__main__":
    main()
