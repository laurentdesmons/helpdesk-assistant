# CLAUDE.md

IT Help Desk Agent Assistant. LangGraph orchestrator + two Microsoft Agent Framework
(MAF) agents (classifier, resolver), all deployed as Foundry Hosted Agents. See
`README.md` for the design doc (living document, updated each phase) and
`.claude/plans/` for the implementation plan.

## Status

**Phases:** 0 scaffold · 0.5 provision · 1 classifier · 2 KB/Search · 3 resolver ·
4 orchestrator+tracing · 4.5 eval gate · 5 escalation queue. Full plan:
`.claude/plans/you-are-my-ai-hidden-fox.md`.

**Done:**
- Phase 0 — scaffold, contracts, config, logging, JSON escalation store, tests.
- Phase 0.5 — Foundry project `helpdesk-dev` in **eastus2** (account
  `cog-isvx3zxptjnfy`), `azd` env `helpdesk-dev`. `.env` populated (App Insights +
  judge still pending). Your account is **Owner** + **Foundry User** at account
  scope (auto-granted at project creation).
- Phase 1 — classifier agent + structured output, `agent_gateway`
  local/fake/remote paths, `scripts/run_local_classifier.py`, 50-row eval set,
  `eval/classifier_eval.py`. **`gpt-4.1-mini` on `FoundryChatClient`**, threshold
  **0.80**, **100%** on the 49-row set. (First deploy used Claude Haiku via
  `AnthropicFoundryClient` — frequent transient `server_error`s in the hosted
  container on the prerelease Anthropic-on-Foundry path + an account-scope RBAC
  grant on the agent MI; swapped to gpt-4.1-mini on the project endpoint: agent MI
  implicit access, one client stack with the resolver, no flakiness.)
- Phase 1 deploy — **`helpdesk-classifier` deployed and verified.** `host.py`
  (`ResponsesHostServer`) + root `main.py` shim, `classifier` service in
  `azure.yaml` (`azd` code-deploy, `remote_build`, `entryPoint: main.py`),
  `RemoteInvoker` (httpx to `.../protocols/openai/responses` + retry/backoff),
  `scripts/verify_deploy.py`, `requirements.txt` + `.azdignore`. Remote eval 100%,
  100% local↔remote parity. Hosted-call latency ~15–18s (cross-region eastus2↔SEA
  + per-call session sandbox) — functional, revisit if it matters.

- Phase 2 — **KB / Azure AI Search layer built and verified end-to-end.**
  `src/helpdesk/search/` (`chunking` pure H2-splitter, `embeddings` =
  `text-embedding-3-small`/1536-d on the account-level `/openai/v1`, `index`
  schema/lifecycle, `client` `KnowledgeBaseSearch.search()` = the Phase 3 seam,
  `pipeline` push-model build). `scripts/build_kb.py` (`--recreate` / `--dry-run`
  / `--category`), `scripts/search_kb.py` (manual query tool). Two indexes
  `support-index` / `hr-index` (`Settings.index_for`; billing → `ValueError`),
  24 chunks (12+12). Hybrid + semantic ranker (`search_query_type`). Hermetic
  `tests/test_chunking.py` + `tests/test_search_config.py`. **Verified** —
  `build_kb.py --recreate` populated both indexes on
  `srch-helpdesk-dev-isvx3zxptjnfy` (Basic, free semantic ranker, AAD auth); all
  six spot queries hit the right doc/section (reranker 2.2–3.1); billing → clean
  `ValueError`; re-run idempotent. Deps: `azure-search-documents>=12.1.0b2` +
  `openai` (local/dev only — `requirements.txt` untouched; the resolver container
  picks them up in Phase 3).
  - **Gotcha:** `text-embedding-3-small` also had a ~50-min post-deploy window
    where the data plane 404'd `DeploymentNotFound` on every route while ARM said
    `Succeeded`; it cleared on its own (no recreate needed).

**Next:** Phase 3 — resolver agent (GPT-5.4-mini) wrapping
`KnowledgeBaseSearch.search()` in an `@ai_function` tool.

**Not committed yet** — all work is untracked on `main`.

## Golden rules

- Python 3.11+. Package manager: `uv` (`uv sync`, `uv run`). `pip install -e ".[dev]"` also works.
- All shared logic lives in `src/helpdesk/`. `scripts/` and `agents/*/host.py` are
  thin entrypoints only — no business logic.
- The JSON contracts are defined once, in `src/helpdesk/contracts.py`. Never
  redefine them per agent.
- Never call a model or Azure service at import time. Agent builders
  (`build_*_agent(settings)`) must be pure constructors.
- Run everything locally with rich logs before deploying. Deploy agents one at a
  time: classifier → resolver → orchestrator, verifying each.
- Deploy gate: `eval/run_all.py` (Phase 4.5) once it exists. Until then the
  interim gate is `python -m eval.classifier_eval --gate --min-accuracy 0.94`
  locally (live model) before deploy, then `HELPDESK_CLASSIFIER_MODE=remote ...
  --gate --min-accuracy 0.90 --compare <local report>` after deploy (parity must
  stay ≥ 95%). gpt-4.1-mini currently scores 100% both ways.

## Layout

- `src/helpdesk/` shared lib: `contracts`, `config`, `logging`, `tracing` (P4),
  `agent_gateway` (P1), `escalation`, `search/` (P2).
- `src/helpdesk/agents/{classifier,resolver,orchestrator}/` — one independently
  deployable agent each: `agent.py` (pure builder + run helpers), `host.py`
  (`*HostServer` entrypoint), `instructions.md`. (Namespaced under `helpdesk` to
  avoid colliding with the installed top-level `agents` package.)
- `main.py` (repo root) — the `codeConfiguration.entryPoint` shim for `azd`
  code-deploy; delegates to the classifier host today.
- `requirements.txt` + `.azdignore` (repo root) — runtime deps + upload excludes
  for `azd deploy` (code mode, `dependencyResolution: remote_build`).
- `eval/` datasets + evaluators (code-based for classifier, Foundry RAG/agent
  evaluators for resolver). Gated; needs live models (`RUN_LIVE_EVAL=1`).
- `scripts/` local drivers with rich logging.
- `docs/` sample KB — **SAMPLE PLACEHOLDER content, pipeline testing only, NOT real
  policy.** Real KB replaces this before production.
- `azure.yaml` + `infra/` are GENERATED by `azd ai agent init` — edit, don't
  hand-author. `tests/` fast + hermetic (no network).

## Local vs deployed (important)

`src/helpdesk/agent_gateway.py` is the seam. `Settings.agent_mode` (env
`HELPDESK_AGENT_MODE`) = `local` | `remote` | `fake`:

- **local**: classifier & resolver run in-process as MAF `Agent` objects.
- **remote**: `RemoteInvoker` calls the deployed Foundry agents by raw async
  httpx POST to each agent's `.../endpoint/protocols/openai/responses` URL, with a
  `DefaultAzureCredential` bearer token (scope `https://ai.azure.com/.default`).
  Phase 4 may switch to `langchain-azure-ai`'s agent node and inject `traceparent`.
- **fake**: deterministic stubs for `tests/`.

The LangGraph graph topology is identical in all modes. Per-agent overrides:
`HELPDESK_CLASSIFIER_MODE`, `HELPDESK_RESOLVER_MODE`.

## Models

- **Classifier**: `gpt-4.1-mini` (Foundry model catalog, portal-managed) via MAF
  `FoundryChatClient` against the **project endpoint**. Structured output =
  `Classification` via `response_format` (baked into the agent `default_options`
  so the hosted `agent.run()` uses it); falls back to prompt-based JSON + Pydantic
  validation + retry. (Was Claude Haiku 4.5 / `AnthropicFoundryClient` — swapped in
  Phase 1 deploy for transient-error and RBAC reasons; see Status.)
- **Embeddings** (Phase 2): `text-embedding-3-small` (1536-d), portal-managed on
  the Foundry project. Called via `AIProjectClient.get_openai_client(base_url=…)`
  pointed at the **account-level** `https://<acct>.services.ai.azure.com/openai/v1`
  — the project-scoped `.../api/projects/<proj>/openai/v1` passthrough proxies
  chat/responses but **NOT `/embeddings`** (bare 404). `search/embeddings.py`
  `_account_openai_v1_url()` derives it from `foundry_project_endpoint`. `-small`
  chosen for the small flat KB; revisit `-large` for the real corpus.
- **Resolver**: GPT-5.4-mini (fallback `gpt-5-mini`) via `FoundryChatClient`.
  Retrieval is a custom `@ai_function` `search_knowledge_base` tool (NOT the context
  provider, NOT the hosted search tool) so tool-call spans + explicit context feed
  the evaluators.
- **Eval judge**: `claude-sonnet-5` or full `gpt-5`. NEVER a `*mini*` model as the
  judge (`eval/judges.py` raises if it sees `mini`). Groundedness score quality
  degrades with judge tier.

## Routing (orchestrator)

`classify → route → resolve | escalate_low_confidence → finalize`.

- confidence < `Settings.confidence_threshold` (0.8, tuned in Phase 1) →
  escalate reason `low_confidence`, regardless of category.
- billing flows through the resolver, which escalates it with reason
  `policy_escalation` (one place owns billing behaviour).
- resolver not grounded → escalate reason `not_grounded`.

Escalation = an `EscalationRecord` written to the store (JSON file dev, Azure Table
prod).

## KB / search

Two indexes: `support-index`, `hr-index`, built from `docs/` frontmatter
`category` (`Settings.index_for`; billing has no index → `ValueError`). Chunked at
H2 sections (`src/helpdesk/search/chunking.py`, pure). **Push model** — embed
locally with `text-embedding-3-small` (1536-d, `Settings.embedding_model` /
`embedding_dimensions`) via the project OpenAI endpoint, `merge_or_upload` whole
docs with vectors; no indexer/skillset. Query = hybrid vector+keyword + semantic
ranker (`Settings.search_query_type` = `vector_semantic_hybrid` | `vector_hybrid`
| `keyword`). `KnowledgeBaseSearch.search(category, query)` (`search/client.py`)
is the Phase 3 resolver seam; `SearchResult.to_citation()` → `contracts.Citation`.
Agentic / Knowledge Base mode is a flag (`Settings.agentic_search`) for when the
real KB arrives. Build: `python scripts/build_kb.py --recreate`
(`--dry-run` chunks only, no network). Query: `python scripts/search_kb.py
--category support --query "..."`. Re-evaluate `-large` + integrated vectorization
when the real KB lands.

## Common commands

```
uv sync                                       # install (dev: uv sync --extra dev)
pytest                                         # fast tests (hermetic)
ruff check . && mypy                            # lint + types
az login  /  azd auth login                     # auth (DefaultAzureCredential)
python scripts/run_local_classifier.py --message "..."
python scripts/run_local_resolver.py --category support --message "..."
python scripts/run_local_graph.py --message "..." [--mode local|remote]
python scripts/build_kb.py --recreate           # build search indexes (--dry-run: chunk only)
python scripts/search_kb.py --category support --query "vpn drops"   # manual KB query
RUN_LIVE_EVAL=1 python -m eval.run_all           # full eval gate (slow, live models)
python scripts/verify_deploy.py <agent>          # post-deploy smoke test
python scripts/verify_trace_propagation.py       # assert one correlated trace
```

## Deployment (azd, one agent at a time)

Unified `azure.yaml` model (one file, an `azure.ai.agent` service per agent — no
separate `manifest.yaml`/`agent.yaml`). Code-deploy mode, `remote_build`.

```
azd ext install azure.ai.agents                  # (+ azure.ai.projects)
azd env select helpdesk-dev
azd env set HELPDESK_CLASSIFIER_MODEL gpt-4.1-mini
python -m helpdesk.agents.classifier.host         # raw local host :8088
azd ai agent run                                  # local host via startupCommand
azd ai agent monitor classifier --follow          # stream logs (needs a session id or --follow)
azd provision --preview  &&  azd provision        # connects to existing helpdesk-dev
azd deploy classifier                             # single service
python scripts/verify_deploy.py classifier        # post-deploy smoke test
```

Order: classifier, then resolver, then orchestrator. Verify each before the next.
Your account is Owner + Foundry User; the deployed agent's MI has implicit
project-endpoint inference access, so with `FoundryChatClient` (project endpoint)
no RBAC grant is needed. Runtime injects `FOUNDRY_PROJECT_ENDPOINT` and
`APPLICATIONINSIGHTS_CONNECTION_STRING` (un-prefixed
— `Settings` bridges them via `AliasChoices`); pass everything else through
`azure.yaml` `env:`.

## Tracing

`AzureAIOpenTelemetryTracer` (from `langchain-azure-ai`) attached to the compiled
graph. All three agents must share one App Insights connection string. Trace-context
propagation across the orchestrator→agent calls is VERIFIED in Phase 4, not
assumed — see `scripts/verify_trace_propagation.py`. View: Azure Monitor →
Investigate → Agents (Preview) for the graph; Foundry portal → Observability →
Traces for agent spans.

## Do not

- Do not commit real secrets. `.env` is gitignored; `.env.example` is the template.
- Do not treat `docs/` as real policy.
- Do not use a mini-tier model as an eval judge.
- Do not add business logic to `scripts/`.
