"""Classifier agent builder.

Pure constructors — no network or model calls at import time. Imported directly by
the orchestrator (local mode) and by ``host.py`` (deployed container).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from azure.identity import DefaultAzureCredential

from helpdesk.config import Settings
from helpdesk.contracts import Classification, ClassifierInput

logger = logging.getLogger("helpdesk.classifier")

_INSTRUCTIONS = (Path(__file__).parent / "instructions.md").read_text("utf-8")

# Appended to the prompt on a retry when the first response didn't parse.
JSON_ONLY_SUFFIX = (
    '\n\nReturn ONLY the JSON object: {"category": "...", "confidence": 0.0, "rationale": "..."}'
)


def build_classifier_agent(settings: Settings) -> Agent:
    """Construct the classifier as an in-process MAF agent on a Foundry GPT model.

    Uses ``FoundryChatClient`` against the project endpoint (OpenAI-family Responses
    API). In the deployed container the endpoint comes from the platform-injected
    ``FOUNDRY_PROJECT_ENDPOINT`` and the call runs as the agent's managed identity,
    which has implicit project-scoped inference access — no extra RBAC grant.
    """
    settings.require("classifier_model", "foundry_project_endpoint")

    client = FoundryChatClient(
        project_endpoint=settings.foundry_project_endpoint,
        model=settings.classifier_model,
        credential=DefaultAzureCredential(),
        allow_preview=True,
    )
    logger.debug(
        "classifier client: endpoint=%s model=%s",
        settings.foundry_project_endpoint,
        settings.classifier_model,
    )
    return Agent(
        client=client,
        name=settings.classifier_agent_name,
        instructions=_INSTRUCTIONS,
        # ``response_format`` lives in the defaults, not just the per-call options
        # in ``classify()``: when the agent is wrapped by a Foundry host server the
        # container calls ``agent.run(messages)`` directly and never goes through
        # ``classify()``, so structured output has to be the default behaviour.
        default_options={"max_tokens": 512, "response_format": Classification},
    )


def build_prompt(req: ClassifierInput) -> str:
    """The user-turn text for a classification request. Shared by local and remote paths."""
    return f"Channel: {req.channel}\nRequest:\n{req.message_text}"


async def classify(agent: Agent, req: ClassifierInput) -> Classification:
    """Run the classifier and return a validated `Classification`.

    Primary path is the model's native structured output (``response.value``).
    Falls back to parsing ``response.text`` as JSON, then to one retry, so a
    transient formatting slip doesn't fail the request.
    """
    response = await agent.run(build_prompt(req), options={"response_format": Classification})
    result: Classification | None = _extract(response)

    if result is None:
        logger.warning(
            "classifier: no structured value and unparseable text for %s; retrying", req.request_id
        )
        retry = await agent.run(
            build_prompt(req) + JSON_ONLY_SUFFIX,
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
