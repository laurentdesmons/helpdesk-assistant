"""Root entry point for `azd` source-code deploy (`codeConfiguration.entryPoint`).

The hosted-agent zip is flat at the root and every agent service deploys the same
zip, so `azd` runs `python main.py` and this shim selects the host by role.
``HELPDESK_AGENT_ROLE`` is set per-service in ``azure.yaml`` (`classifier` |
`resolver`); it defaults to `classifier`. This is routing, not business logic —
it reads the env var directly to stay dependency-free.
"""

from __future__ import annotations

import os


def main() -> None:
    role = os.environ.get("HELPDESK_AGENT_ROLE", "classifier").strip().lower()
    if role == "resolver":
        from helpdesk.agents.resolver.host import main as run
    elif role == "classifier":
        from helpdesk.agents.classifier.host import main as run
    else:
        raise SystemExit(f"unknown HELPDESK_AGENT_ROLE {role!r} (expected 'classifier' or 'resolver')")
    run()


if __name__ == "__main__":
    main()
