"""Independently deployable Foundry agents.

Each subpackage (`classifier`, `resolver`, `orchestrator`) is one azd service with
its own `agent.py` builder, `host.py` entrypoint, and deploy config. They live
under the `helpdesk` namespace (not a top-level `agents/`) to avoid colliding with
third-party packages that also import as `agents`.
"""
