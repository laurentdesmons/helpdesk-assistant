"""Runtime configuration.

One ``Settings`` object, populated from environment variables (prefix
``HELPDESK_``) or a local ``.env`` file. Azure-specific fields are optional so
the Phase 0 test suite and ``import helpdesk`` work with nothing configured;
each agent asserts the fields it actually needs at build time.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

AgentMode = Literal["local", "remote", "fake"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HELPDESK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Foundry project (Phase 0.5) --------------------------------------
    foundry_project_endpoint: str | None = None
    azure_ai_project_endpoint: str | None = None
    applicationinsights_connection_string: str | None = None

    # --- Classifier: Claude Haiku 4.5 (Phase 1) -------------------------
    anthropic_foundry_resource: str | None = None
    anthropic_foundry_api_key: str | None = None
    classifier_model: str = "claude-haiku-4-5"

    # --- Resolver: GPT-5.4-mini (Phase 3) ------------------------------
    resolver_model: str = "gpt-5.4-mini"
    resolver_model_fallback: str = "gpt-5-mini"

    # --- Evaluation judge (Phase 3) — must not be a *mini* model ---------
    judge_model: str = "claude-sonnet-5"

    # --- Azure AI Search (Phase 2) ------------------------------------
    search_endpoint: str | None = None
    embedding_model: str = "text-embedding-3-large"
    support_index: str = "support-index"
    hr_index: str = "hr-index"
    search_query_type: str = "vector_semantic_hybrid"
    agentic_search: bool = False

    # --- Agent identity (deployed names) --------------------------------
    classifier_agent_name: str = "helpdesk-classifier"
    resolver_agent_name: str = "helpdesk-resolver"
    orchestrator_agent_name: str = "helpdesk-orchestrator"

    # --- Behaviour ------------------------------------------------------
    agent_mode: AgentMode = "local"
    classifier_mode: AgentMode | None = None
    resolver_mode: AgentMode | None = None
    # Tuned in Phase 1: on the 49-row labeled set, correct predictions scored
    # >= 0.85, the one misclassification 0.75, ambiguous requests < 0.6.
    confidence_threshold: float = Field(default=0.8, ge=0.0, le=1.0)

    # --- Escalation store ---------------------------------------------
    escalation_store: Literal["json", "table"] = "json"
    escalation_store_path: str = ".local/escalations.json"

    log_level: str = "INFO"

    def resolve_anthropic_resource(self) -> str:
        """The Foundry resource sub-domain for the Anthropic (Claude) endpoint.

        Uses ``anthropic_foundry_resource`` when set, otherwise derives it from
        the host of ``foundry_project_endpoint`` (``https://<resource>.services.
        ai.azure.com/...`` -> ``<resource>``). The deployed container only gets
        ``FOUNDRY_PROJECT_ENDPOINT`` injected, so the derivation keeps the agent
        working without an extra env var.
        """
        if self.anthropic_foundry_resource:
            return self.anthropic_foundry_resource
        if self.foundry_project_endpoint:
            host = urlparse(self.foundry_project_endpoint).hostname or ""
            if host.endswith(".services.ai.azure.com"):
                return host.split(".", 1)[0]
        raise RuntimeError(
            "Cannot resolve the Anthropic Foundry resource. Set "
            "HELPDESK_ANTHROPIC_FOUNDRY_RESOURCE or HELPDESK_FOUNDRY_PROJECT_ENDPOINT."
        )

    def mode_for(self, agent: Literal["classifier", "resolver"]) -> AgentMode:
        """Effective mode for one agent — per-agent override falls back to ``agent_mode``."""
        override = self.classifier_mode if agent == "classifier" else self.resolver_mode
        return override or self.agent_mode

    def require(self, *names: str) -> None:
        """Raise if any named setting is unset — call this in agent builders."""
        missing = [n for n in names if getattr(self, n, None) in (None, "")]
        if missing:
            names_str = ", ".join("HELPDESK_" + n.upper() for n in missing)
            raise RuntimeError(
                f"Missing required settings: {names_str}. Set them in .env (see .env.example)."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
