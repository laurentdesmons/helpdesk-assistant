"""Root entry point for `azd` source-code deploy (`codeConfiguration.entryPoint`).

The hosted-agent zip is flat at the root, so `azd` runs `python main.py` from the
package root. This shim just delegates to the classifier host. When the resolver
and orchestrator get their own hosted-agent services (Phase 3+), branch here on an
env var (e.g. ``HELPDESK_AGENT_ROLE``).
"""

from __future__ import annotations

from helpdesk.agents.classifier.host import main

if __name__ == "__main__":
    main()
