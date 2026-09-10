"""Resolver agent builder + run helpers.

Pure constructors — no network or model calls at import time. Imported directly by
the orchestrator (local mode) and by ``host.py`` (deployed container).

The resolver answers ``support`` / ``hr`` requests by RAG over the KB and escalates
everything it can't ground. ``billing`` never reaches the model — the gateway and
``resolve()`` short-circuit it to ``policy_escalation`` before any tool or model
call (README §4: "No resolution attempt").
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated

from agent_framework import Agent, FunctionTool, tool
from agent_framework.foundry import FoundryChatClient
from azure.identity import DefaultAzureCredential
from pydantic import BaseModel, ConfigDict

from helpdesk.config import Settings
from helpdesk.contracts import Category, EscalationReason, ResolverInput, ResolverOutput
from helpdesk.search.client import KnowledgeBaseSearch, build_kb_search

logger = logging.getLogger("helpdesk.resolver")

_INSTRUCTIONS = (Path(__file__).parent / "instructions.md").read_text("utf-8")
_MAX_SNIPPET = 500

# Appended to the prompt on a retry when the first response didn't parse.
JSON_ONLY_SUFFIX = (
    "\n\nReturn ONLY the JSON object: "
    '{"request_id": "...", "status": "answered|escalated", "answer": "... or null", '
    '"citations": [{"doc_id": "...", "title": "...", "snippet": "...", "score": 0.0}], '
    '"escalation_reason": "not_grounded|policy_escalation|low_confidence or null"}'
)


# --------------------------------------------------------------------------- #
# Structured-output draft models
#
# ``ResolverOutput`` / ``Citation`` carry ``Field(min_length=1)`` and a cross-field
# validator. ``agent_framework_openai`` sends the ``response_format`` schema with
# ``strict: True``, and strict structured-output validators are picky about
# constraint keywords (this is exactly why ``Classification`` was kept
# constraint-free). We hand the model a constraint-free *draft* schema and
# re-validate into the real contract client-side — the guarantees come back, the
# wire schema stays plain.
# --------------------------------------------------------------------------- #
class _CitationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: str
    title: str | None = None
    snippet: str | None = None
    score: float | None = None


class _ResolverDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    status: str
    answer: str | None = None
    citations: list[_CitationDraft] = []
    escalation_reason: str | None = None


def make_search_tool(kb: KnowledgeBaseSearch) -> FunctionTool:
    """A ``search_knowledge_base`` tool wrapping ``KnowledgeBaseSearch.search()``.

    Takes a pre-built KB so the caller owns its lifetime — the deployed host lets
    it live for the process; local drivers / eval close it via
    ``LocalInvoker.aclose()``. ``build_kb_search`` is a pure constructor.
    """

    @tool(
        name="search_knowledge_base",
        description=(
            "Search the IT help-desk knowledge base for policy / how-to snippets. "
            "Call this before answering. Returns ranked snippets, each with a "
            "doc_id and title to cite. If it returns no results, escalate."
        ),
    )
    async def search_knowledge_base(
        query: Annotated[str, "A focused, keyword-rich search query from the user's request."],
        category: Annotated[
            str, "'support' or 'hr' — exactly as given in the prompt. Never 'billing'."
        ],
    ) -> str:
        try:
            results = await kb.search(category, query)
        except ValueError as exc:  # unknown / billing category — defensive
            logger.warning("search_knowledge_base bad category %r: %s", category, exc)
            return json.dumps({"error": str(exc), "count": 0, "results": []})
        except Exception as exc:  # noqa: BLE001 — surface as a tool result, not a crash
            logger.error("search_knowledge_base failed [%s] %r: %s", category, query, exc)
            return json.dumps(
                {"error": f"{type(exc).__name__}: {exc}", "count": 0, "results": []}
            )
        payload = [
            {
                "doc_id": r.doc_id,
                "title": f"{r.title} > {r.section}",
                "snippet": r.content[:_MAX_SNIPPET],
                "score": round(
                    r.reranker_score if r.reranker_score is not None else r.score, 3
                ),
            }
            for r in results
        ]
        logger.info("search_knowledge_base[%s] %r -> %d hits", category, query, len(payload))
        return json.dumps({"count": len(payload), "results": payload})

    return search_knowledge_base


def build_resolver_agent(settings: Settings, *, kb: KnowledgeBaseSearch | None = None) -> Agent:
    """Construct the resolver as an in-process MAF agent on a Foundry GPT model.

    ``kb`` — a pre-built :class:`KnowledgeBaseSearch` whose lifetime the caller
    owns; when omitted a fresh one is built (deployed host: lives for the process).

    ``response_format`` and ``tool_choice`` live in ``default_options``, not just
    per-call: the Foundry host server calls ``agent.run(messages)`` directly and
    drops per-request options, so structured output + forced first-turn retrieval
    have to be the agent defaults. ``tool_choice="required"`` auto-resets to auto
    after the first tool turn (``agent_framework._tools._reset_required_tool_choice``),
    so later turns are free to emit the answer.
    """
    settings.require("resolver_model", "foundry_project_endpoint", "search_endpoint")

    client = FoundryChatClient(
        project_endpoint=settings.foundry_project_endpoint,
        model=settings.resolver_model,
        credential=DefaultAzureCredential(),
        allow_preview=True,
    )
    logger.debug(
        "resolver client: endpoint=%s model=%s search=%s",
        settings.foundry_project_endpoint,
        settings.resolver_model,
        settings.search_endpoint,
    )
    return Agent(
        client=client,
        name=settings.resolver_agent_name,
        instructions=_INSTRUCTIONS,
        tools=[make_search_tool(kb or build_kb_search(settings))],
        default_options={
            "response_format": _ResolverDraft,
            "tool_choice": "required",
        },
    )


def build_prompt(req: ResolverInput) -> str:
    """The user-turn text for a resolve request. Shared by local and remote paths."""
    return (
        f"request_id: {req.request_id}\n"
        f"Category: {req.category}\n"
        f"User request:\n{req.message_text}"
    )


async def resolve(agent: Agent, req: ResolverInput) -> ResolverOutput:
    """Run the resolver and return a validated `ResolverOutput`.

    Billing short-circuits before any model or tool call. Otherwise: native
    structured output, then a text-parse fallback, then one retry, then a
    not-grounded escalation so a formatting slip never fails the request.
    """
    if str(req.category) == Category.billing:
        return _escalate(req, "policy_escalation")

    result = await _run_once(agent, build_prompt(req), req)
    if result is None:
        logger.warning("resolver: unparseable output for %s; retrying", req.request_id)
        result = await _run_once(agent, build_prompt(req) + JSON_ONLY_SUFFIX, req)
    if result is None:
        logger.warning(
            "resolver: no parseable ResolverOutput for %s; escalating not_grounded",
            req.request_id,
        )
        return _escalate(req, "not_grounded")

    return _guard(result, req)


async def _run_once(agent: Agent, prompt: str, req: ResolverInput) -> ResolverOutput | None:
    try:
        response = await agent.run(prompt, options={"response_format": _ResolverDraft})
    except Exception as exc:  # noqa: BLE001 — surface as "unparseable" so retry/fallback runs
        logger.warning("resolver run error for %s: %s", req.request_id, exc)
        return None
    return _extract(response, req)


def _escalate(req: ResolverInput, reason: EscalationReason) -> ResolverOutput:
    return ResolverOutput(request_id=req.request_id, status="escalated", escalation_reason=reason)


def _extract(response: object, req: ResolverInput) -> ResolverOutput | None:
    """Native structured output first (``response.value``), then parse ``response.text``."""
    value = getattr(response, "value", None)
    if isinstance(value, _ResolverDraft):
        return _from_draft(value, req)
    if isinstance(value, ResolverOutput):
        return value
    return _parse_text(getattr(response, "text", None), req)


def _parse_text(text: str | None, req: ResolverInput) -> ResolverOutput | None:
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(data, dict):
        data.setdefault("request_id", req.request_id)
    try:
        return ResolverOutput.model_validate(data)
    except ValueError:
        return None


def _from_draft(draft: _ResolverDraft, req: ResolverInput) -> ResolverOutput | None:
    data = draft.model_dump()
    data.setdefault("request_id", req.request_id)
    try:
        return ResolverOutput.model_validate(data)
    except ValueError as exc:
        logger.warning("resolver: draft failed contract validation for %s: %s", req.request_id, exc)
        return None


def _guard(result: ResolverOutput, req: ResolverInput) -> ResolverOutput:
    """Post-hoc invariants the model can't be trusted to hold.

    - request_id must match the request.
    - ``answered`` with no citations is not grounded — downgrade to escalation.
    """
    if result.request_id != req.request_id:
        result = result.model_copy(update={"request_id": req.request_id})
    if result.status == "answered" and not result.citations:
        logger.info(
            "resolver: answered without citations for %s -> not_grounded", req.request_id
        )
        return _escalate(req, "not_grounded")
    return result
