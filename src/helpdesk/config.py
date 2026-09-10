"""Runtime configuration.

One ``Settings`` object, populated from environment variables (prefix
``HELPDESK_``) or a local ``.env`` file. Azure-specific fields are optional so
the Phase 0 test suite and ``import helpdesk`` work with nothing configured;
each agent asserts the fields it actually needs at build time.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

AgentMode = Literal["local", "remote", "fake"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HELPDESK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        # An empty env var (e.g. an unresolved ``${VAR}`` in azure.yaml) must fall
        # back to the field default, not override it with "".
        env_ignore_empty=True,
    )

    # --- Foundry project (Phase 0.5) --------------------------------------
    # The deployed container gets the bare (un-prefixed) ``FOUNDRY_PROJECT_ENDPOINT``
    # / ``APPLICATIONINSIGHTS_CONNECTION_STRING`` injected by the Foundry runtime;
    # locally we set the ``HELPDESK_``-prefixed names. An explicit ``validation_alias``
    # turns off ``env_prefix`` for that field, so the prefixed name is listed too.
    foundry_project_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "HELPDESK_FOUNDRY_PROJECT_ENDPOINT", "FOUNDRY_PROJECT_ENDPOINT"
        ),
    )
    azure_ai_project_endpoint: str | None = None
    applicationinsights_connection_string: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "HELPDESK_APPLICATIONINSIGHTS_CONNECTION_STRING",
            "APPLICATIONINSIGHTS_CONNECTION_STRING",
        ),
    )

    # --- Classifier: Foundry GPT model via FoundryChatClient (Phase 1) ---
    classifier_model: str = "gpt-4.1-mini"

    # --- Resolver: GPT-5.4-mini (Phase 3) ------------------------------
    resolver_model: str = "gpt-5.4-mini"
    resolver_model_fallback: str = "gpt-5-mini"

    # --- Evaluation judge (Phase 3) — must not be a *mini* model ---------
    judge_model: str = "claude-sonnet-5"

    # --- Azure AI Search (Phase 2) ------------------------------------
    search_endpoint: str | None = None
    # -small (1536-d) is ample for the small, flat, lexically-distinct KB; the
    # hybrid + semantic ranker carries retrieval. Re-evaluate -large when the
    # real (larger, multi-section) KB replaces the placeholder docs.
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    support_index: str = "support-index"
    hr_index: str = "hr-index"
    search_semantic_config: str = "helpdesk-semantic"
    search_top_k: int = 4
    # vector_semantic_hybrid (default) | vector_hybrid | keyword — lets eval
    # ablate the retrieval stack.
    search_query_type: str = "vector_semantic_hybrid"
    agentic_search: bool = False

    # --- Agent identity (deployed names) --------------------------------
    classifier_agent_name: str = "helpdesk-classifier"
    resolver_agent_name: str = "helpdesk-resolver"
    orchestrator_agent_name: str = "helpdesk-orchestrator"

    # --- Deployed agent endpoints (Phase 1 deploy) ---------------------
    # ``azd`` writes ``AGENT_CLASSIFIER_RESPONSES_ENDPOINT`` to the azd env after
    # deploy; locally, put it in ``.env`` as ``HELPDESK_CLASSIFIER_AGENT_ENDPOINT``.
    # Left unset, ``classifier_responses_url()`` derives it from the project
    # endpoint + agent name.
    classifier_agent_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "HELPDESK_CLASSIFIER_AGENT_ENDPOINT", "AGENT_CLASSIFIER_RESPONSES_ENDPOINT"
        ),
    )
    resolver_agent_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "HELPDESK_RESOLVER_AGENT_ENDPOINT", "AGENT_RESOLVER_RESPONSES_ENDPOINT"
        ),
    )
    orchestrator_agent_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "HELPDESK_ORCHESTRATOR_AGENT_ENDPOINT", "AGENT_ORCHESTRATOR_RESPONSES_ENDPOINT"
        ),
    )

    # --- Behaviour ------------------------------------------------------
    # Entry-point role for the shared ``main.py`` deploy shim; set per-service in
    # ``azure.yaml``. Only matters when a single zip hosts more than one agent.
    agent_role: Literal["classifier", "resolver", "orchestrator"] = "classifier"
    agent_mode: AgentMode = "local"
    classifier_mode: AgentMode | None = None
    resolver_mode: AgentMode | None = None
    # Tuned in Phase 1: on the 49-row labeled set, correct predictions scored
    # >= 0.85, the one misclassification 0.75, ambiguous requests < 0.6.
    confidence_threshold: float = Field(default=0.8, ge=0.0, le=1.0)

    # --- Tracing (Phase 4) -------------------------------------------
    # AzureAIOpenTelemetryTracer is attached to the orchestrator graph. With an
    # App Insights connection string it exports spans; without one it still emits
    # spans on the local TracerProvider (so ``trace=`` shows in logs and W3C
    # ``traceparent`` is injected on the deployed-agent calls). Off => no callback.
    tracing_enabled: bool = True
    trace_content_recording: bool = True
    trace_sampling_ratio: float = Field(default=1.0, ge=0.0, le=1.0)

    # --- Escalation store ---------------------------------------------
    escalation_store: Literal["json", "table"] = "json"
    escalation_store_path: str = ".local/escalations.json"

    log_level: str = "INFO"

    def _agent_responses_url(self, explicit: str | None, agent_name: str) -> str:
        """The `POST /responses` URL of a deployed agent.

        Prefers ``explicit`` — which ``azd`` writes to the env as
        ``AGENT_<ROLE>_RESPONSES_ENDPOINT`` already fully-formed (with its
        ``?api-version`` query). Only a bare agent base URL gets ``/responses``
        appended. Falls back to deriving from the project endpoint + agent name.
        """
        if explicit:
            parts = urlsplit(explicit)
            path = parts.path.rstrip("/")
            if not path.endswith("/responses"):
                path = f"{path}/responses"
            return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))
        self.require("foundry_project_endpoint")
        assert self.foundry_project_endpoint is not None  # narrowed by require()
        project = self.foundry_project_endpoint.rstrip("/")
        return (
            f"{project}/agents/{agent_name}"
            "/endpoint/protocols/openai/responses?api-version=v1"
        )

    def classifier_responses_url(self) -> str:
        """The `POST /responses` URL of the deployed classifier agent."""
        return self._agent_responses_url(self.classifier_agent_endpoint, self.classifier_agent_name)

    def resolver_responses_url(self) -> str:
        """The `POST /responses` URL of the deployed resolver agent."""
        return self._agent_responses_url(self.resolver_agent_endpoint, self.resolver_agent_name)

    def orchestrator_responses_url(self) -> str:
        """The `POST /responses` URL of the deployed orchestrator agent."""
        return self._agent_responses_url(
            self.orchestrator_agent_endpoint, self.orchestrator_agent_name
        )

    def index_for(self, category: str) -> str:
        """Search index name for a category. ``billing`` has no KB — it escalates."""
        mapping = {"support": self.support_index, "hr": self.hr_index}
        try:
            return mapping[str(category)]
        except KeyError:
            raise ValueError(
                f"no knowledge-base index for category {category!r}; "
                "billing requests escalate rather than retrieve"
            ) from None

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
