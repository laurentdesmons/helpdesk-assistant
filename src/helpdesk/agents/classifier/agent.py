"""Classifier agent builder.

Pure constructors — no network or model calls at import time. Imported directly by
the orchestrator (local mode) and by ``host.py`` (deployed container).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent_framework import Agent
from agent_framework.anthropic import AnthropicFoundryClient
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

from helpdesk.config import Settings
from helpdesk.contracts import Classification, ClassifierInput

logger = logging.getLogger("helpdesk.classifier")

# Entra ID scope for Foundry data-plane (model inference) calls.
_FOUNDRY_SCOPE = "https://cognitiveservices.azure.com/.default"

_INSTRUCTIONS = (Path(__file__).parent / "instructions.md").read_text("utf-8")


def build_classifier_agent(settings: Settings) -> Agent:
    """Construct the classifier as an in-process MAF agent on Claude Haiku 4.5."""
    settings.require("classifier_model")
    resource = settings.resolve_anthropic_resource()

    client = AnthropicFoundryClient(
        resource=resource,
        model=settings.classifier_model,
        azure_ad_token_provider=get_bearer_token_provider(DefaultAzureCredential(), _FOUNDRY_SCOPE),
    )
    logger.debug("classifier client: resource=%s model=%s", resource, settings.classifier_model)
    return Agent(
        client=client,
        name=settings.classifier_agent_name,
        instructions=_INSTRUCTIONS,
        default_options={"max_tokens": 512},
    )


def _prompt(req: ClassifierInput) -> str:
    return f"Channel: {req.channel}\nRequest:\n{req.message_text}"


async def classify(agent: Agent, req: ClassifierInput) -> Classification:
    """Run the classifier and return a validated `Classification`.

    Primary path is Claude's native structured output (``response.value``). Falls
    back to parsing ``response.text`` as JSON, then to one retry, so a transient
    formatting slip doesn't fail the request.
    """
    response = await agent.run(_prompt(req), options={"response_format": Classification})
    result: Classification | None = _extract(response)

    if result is None:
        logger.warning(
            "classifier: no structured value and unparseable text for %s; retrying", req.request_id
        )
        retry = await agent.run(
            _prompt(req) + "\n\nReturn ONLY the JSON object: "
            '{"category": "...", "confidence": 0.0, "rationale": "..."}',
            options={"response_format": Classification},
        )
        result = _extract(retry)

    if result is None:
        raise ValueError(f"classifier returned no parseable Classification for {req.request_id!r}")

    logger.info(
        "classified %s -> %s (%.2f) %s",
        req.request_id,
        result.category,
        result.confidence,
        result.rationale,
    )
    return result


def _extract(response: object) -> Classification | None:
    """Native structured output first (``response.value``), then parse ``response.text``."""
    value = getattr(response, "value", None)
    if isinstance(value, Classification):
        return value
    return _parse_text(getattr(response, "text", None))


def _parse_text(text: str | None) -> Classification | None:
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`").removeprefix("json").strip()
    try:
        return Classification.model_validate(json.loads(candidate))
    except (json.JSONDecodeError, ValueError):
        return None
