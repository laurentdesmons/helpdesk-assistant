"""Container entrypoint for the Foundry-hosted classifier.

Thin: no business logic. Builds the same MAF ``Agent`` the local path uses and
serves it over the Responses protocol (``POST /responses`` + ``GET /readiness``,
port ``PORT`` env or 8088).

    python -m helpdesk.agents.classifier.host      # local host on :8088

The Foundry runtime runs this via the ``startupCommand`` / ``entryPoint`` in
``azure.yaml`` and injects ``FOUNDRY_PROJECT_ENDPOINT`` (+ ``APPLICATIONINSIGHTS_
CONNECTION_STRING`` once App Insights is wired in Phase 4).
"""

from __future__ import annotations

from agent_framework_foundry_hosting import ResponsesHostServer

from helpdesk.agents.classifier.agent import build_classifier_agent
from helpdesk.config import get_settings
from helpdesk.logging import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    agent = build_classifier_agent(settings)  # pure constructor — no model call
    ResponsesHostServer(agent).run()


if __name__ == "__main__":
    main()
