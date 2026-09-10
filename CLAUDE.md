# CLAUDE.md

IT Help Desk Agent Assistant. LangGraph orchestrator + two Microsoft Agent Framework
(MAF) agents (classifier, resolver), all deployed as Foundry Hosted Agents. See
`README.md` for the design doc (living document, updated each phase) and
`.claude/plans/` for the implementation plan.

## Status

**Phases:** 0 scaffold · 0.5 provision · 1 classifier · 2 KB/Search · 3 resolver ·
4 orchestrator+tracing · 4.5 eval gate. Full plan:
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
  + per-call session sandbox) — functional.

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

- Phase 3 — **`helpdesk-resolver` deployed and verified.** Built + wired
  (local/fake/remote).
  `src/helpdesk/agents/resolver/` (`agent.py` pure builder + `make_search_tool` +
  `resolve` run helper, `host.py`, `instructions.md`). **`gpt-5.4-mini` on
  `FoundryChatClient`** (project endpoint). Retrieval = a custom MAF `@tool`
  `search_knowledge_base` wrapping `KnowledgeBaseSearch.search()` (returns JSON
  snippets). `default_options` bakes in `response_format` + `tool_choice="required"`
  (auto-resets to auto after the first tool turn) so the Foundry host — which drops
  per-request options — still forces one grounded retrieval. Structured output uses
  a constraint-free `_ResolverDraft` re-validated into `ResolverOutput` client-side
  (strict schema rejects `min_length`, same reason `Classification` is
  constraint-free). Billing short-circuits to `policy_escalation` in every invoker
  before any model/search call; `answered` with no citations → `not_grounded`
  guard. `main.py` now branches on `HELPDESK_AGENT_ROLE` (classifier|resolver) —
  both agents deploy the same zip. `resolver` service + RBAC notes in `azure.yaml`;
  `scripts/run_local_resolver.py`, `verify_deploy.py resolver`,
  `eval/resolver_eval.py` (code-based: routing accuracy + billing sub-gate +
  not-grounded recall + citation validity; no judge — real groundedness still
  blocked on a real KB) + `eval/datasets/resolver_labeled.jsonl` (35 rows).
  `requirements.txt` gains `azure-search-documents` + `openai`. **`gpt-5.4-mini`
  scores 100% on the 35-row set both ways** (routing accuracy, billing sub-gate,
  not-grounded P/R, citation validity, expected-doc hit — all 100%), **100%
  local↔remote parity (35/35)**. Latency ~5s local, ~15–24s remote. Resolver MI
  granted `Search Index Data Reader` (Search service) + `Cognitive Services OpenAI
  User` (account scope, for the embeddings `/openai/v1` route). Deploy gotcha
  (empty `${VAR}` → `model=""` → 404) fixed with `env_ignore_empty=True` +
  trimmed `azure.yaml` env — see Deployment.

- Phase 4 — **`helpdesk-orchestrator` deployed and verified; trace propagation
  VERIFIED.** `src/helpdesk/agents/orchestrator/` (`graph.py` pure
  `build_graph(settings, invoker=…)` + `run_graph`; `host.py` `_GraphAgent`
  `SupportsAgentRun` shim over the compiled graph for `ResponsesHostServer`;
  `instructions.md`). Nodes `classify → route → resolve | escalate_low_confidence
  → finalize`; `route` is a pass-through, the conditional edge sends
  `confidence < confidence_threshold` (0.8) → `escalate_low_confidence` (any
  category), else `resolve`. Billing is **not** special-cased in the graph — the
  `agent_gateway` seam short-circuits it. `finalize` is the single I/O point:
  builds `HelpdeskResult`, writes an `EscalationRecord` on any escalation
  (**non-fatally** — a failed store write logs + still returns the result).
  `src/helpdesk/tracing.py` (`build_tracer` = `AzureAIOpenTelemetryTracer`, no-op
  export without an App Insights conn string but still emits spans;
  `configure_tracing` installs an OTel-SDK `TracerProvider` +
  `TraceIdRatioBased` sampler). `agent_gateway` gains `inject(headers)` on both
  `RemoteInvoker` calls + `OrchestratorClient` (W3C `traceparent`),
  `CompositeInvoker`, `build_graph_invoker(settings, mode=None)`, and
  `OrchestratorClient` (calls the deployed graph end-to-end → `HelpdeskResult`).
  `RemoteInvoker` now retries `httpx` transport errors (cross-region read
  timeouts), not just `status: failed`. `scripts/run_local_graph.py`,
  `eval/orchestrator_eval.py` (code-based: outcome accuracy + billing sub-gate +
  low_confidence recall + citation validity + escalation-record written) +
  `eval/datasets/orchestrator_scenarios.jsonl` (14 rows, all 5 outcomes),
  `verify_deploy.py orchestrator`, `scripts/verify_trace_propagation.py`.
  `agent_role` widened to `orchestrator`; `main.py` branch; `orchestrator` service
  in `azure.yaml`. `pyproject.toml` / `requirements.txt` gain `langgraph`,
  `langchain-azure-ai[opentelemetry]`, `opentelemetry-sdk` (+ `azure-monitor-query`
  in `[dev]`). **Fake-invoker classifier confidence bumped to ≥ 0.85 on a keyword
  hit** (a clear match must not trip low_confidence); the fake resolver cites real
  `docs/` doc_ids.
  - **Deploy** — App Insights `appi-helpdesk-dev` (workspace-based on
    `log-helpdesk-dev`) connected to the project → runtime injects
    `APPLICATIONINSIGHTS_CONNECTION_STRING` into every agent; classifier +
    resolver redeployed (v3) to pick it up. Orchestrator = hosted agent #3,
    **2 CPU / 4 Gi**, `HELPDESK_AGENT_MODE=remote`, sibling endpoints from
    `${AGENT_{CLASSIFIER,RESOLVER}_RESPONSES_ENDPOINT}`. **No RBAC grant needed** —
    the orchestrator MI's implicit project access covers the sibling-agent calls.
  - **Gotchas** — (1) the hosted-agent container root FS is **read-only**: the JSON
    escalation store `mkdir`/`FileLock` threw → every *escalated* request 500'd
    (answered ones were fine). Fix: `HELPDESK_ESCALATION_STORE_PATH=/tmp/helpdesk/
    escalations.json` in `azure.yaml` + the `finalize` write is now non-fatal.
    (2) `AzureAIOpenTelemetryTracer` sets no resource `service.name` → the
    orchestrator's graph-node spans landed as `cloud_RoleName="unknown_service"`.
    Fix: `OTEL_SERVICE_NAME=helpdesk-orchestrator` in `azure.yaml`.
  - **Verified** — `verify_deploy.py orchestrator` 5/5 through the deployed graph;
    `orchestrator_eval` 100% (fake + in-process) with 100% local↔remote parity
    (14/14); `verify_trace_propagation.py` → one App Insights `operation_Id` spans
    the orchestrator's node spans, the classifier's `chat gpt-4.1-mini`, and the
    resolver's `chat gpt-5.4-mini` + `execute_tool search_knowledge_base`.

- Phase 4.5 — **`eval/run_all.py` consolidated deploy gate.** One command, one
  exit code over all three code-based suites (`classifier_eval` → `resolver_eval`
  → `orchestrator_eval`). Subprocesses each suite's own `--gate` mode
  (`[sys.executable, "-m", "eval.<suite>", …]`), streams their rich output, reads
  back the `.local/eval/<suite>_*.json` reports, prints a PASS/FAIL summary +
  writes `.local/eval/run_all_<stamp>.json`, exits non-zero if any suite fails or
  wrote no fresh report. `--only classifier,resolver,orchestrator` subsets;
  `--remote` sets `HELPDESK_{CLASSIFIER,RESOLVER}_MODE=remote`, relaxes the
  accuracy floors (0.90/0.85/0.85), and `--compare`s each suite against its
  newest prior local report (parity ≥ `--min-parity`, default 0.95); `--min-*`
  flags override per suite. No metrics of its own — pure orchestration. Not wired
  into `azd` (no `hooks.predeploy`) — run it by hand pre-deploy. No CI.

## Golden rules

- Python 3.11+. Package manager: `uv` (`uv sync`; `uv sync --extra dev` for tests).
  **Always run scripts, modules, and tools through `uv run`** — `uv run python
  scripts/…`, `uv run python -m eval.…`, `uv run pytest`, `uv run ruff`, `uv run
  mypy` — never bare `python`/`pytest` (the sole exception is `azure.yaml`'s
  `startupCommand: python main.py`, which runs inside the Foundry container where
  there is no `uv`). `pip install -e ".[dev]"` also works for setup.
- All shared logic lives in `src/helpdesk/`. `scripts/` and `agents/*/host.py` are
  thin entrypoints only — no business logic.
- The JSON contracts are defined once, in `src/helpdesk/contracts.py`. Never
  redefine them per agent.
- Never call a model or Azure service at import time. Agent builders
  (`build_*_agent(settings)`) must be pure constructors.
- Run everything locally with rich logs before deploying. Deploy agents one at a
  time: classifier → resolver → orchestrator, verifying each.
- Deploy gate: **`uv run python -m eval.run_all`** before every `azd deploy`
  (local, live models), then **`uv run python -m eval.run_all --remote`** after
  (graph in-process, agents deployed — the parity pass). One non-zero exit gates
  all three suites. Post-deploy, also run `verify_deploy.py <agent>` and
  `verify_trace_propagation.py`. To debug one suite in isolation, run it directly
  (`run_all` just wraps these):
  - classifier — `uv run python -m eval.classifier_eval --gate --min-accuracy 0.94`,
    then `HELPDESK_CLASSIFIER_MODE=remote ... --gate --min-accuracy 0.90 --compare
    <local report>` (parity ≥ 95%). gpt-4.1-mini scores 100% both ways.
  - resolver — `uv run python -m eval.resolver_eval --gate --min-routing-accuracy
    0.90` (code-based: routing accuracy, billing sub-gate, not-grounded recall,
    citation validity), then `HELPDESK_RESOLVER_MODE=remote ... --min-routing-accuracy
    0.85 --compare <local report>`.
  - orchestrator — `uv run python -m eval.orchestrator_eval --gate
    --min-outcome-accuracy 0.90` (outcome accuracy, billing sub-gate,
    low_confidence recall, citation validity, escalation-record written), then
    `HELPDESK_CLASSIFIER_MODE=remote HELPDESK_RESOLVER_MODE=remote ...
    --min-outcome-accuracy 0.85 --compare <local report>` (graph in-process,
    agents deployed — the 100%-parity check). Post-deploy: `verify_deploy.py
    orchestrator` (whole graph through the deployed endpoint) +
    `verify_trace_propagation.py`.

## Layout

- `src/helpdesk/` shared lib: `contracts`, `config`, `logging`, `tracing` (P4 —
  `build_tracer` + `configure_tracing`), `agent_gateway` (P1 — invokers +
  `CompositeInvoker` / `build_graph_invoker` + `OrchestratorClient`),
  `escalation`, `search/` (P2).
- `src/helpdesk/agents/{classifier,resolver}/` — `agent.py` (pure builder + run
  helpers), `host.py` (`ResponsesHostServer` entrypoint), `instructions.md`.
  `src/helpdesk/agents/orchestrator/` — `graph.py` (pure `build_graph` +
  `run_graph`, no `agent.py`), `host.py` (`_GraphAgent` shim + `ResponsesHostServer`),
  `instructions.md`. (Namespaced under `helpdesk` to avoid colliding with the
  installed top-level `agents` package.)
- `main.py` (repo root) — the `codeConfiguration.entryPoint` shim for `azd`
  code-deploy; branches on `HELPDESK_AGENT_ROLE` (`classifier` | `resolver` |
  `orchestrator`) — all three services deploy the same zip.
- `requirements.txt` + `.azdignore` (repo root) — runtime deps + upload excludes
  for `azd deploy` (code mode, `dependencyResolution: remote_build`).
- `eval/` datasets + evaluators (code-based for classifier, resolver, and
  orchestrator alike; the resolver's `--judge` groundedness path is scaffolded
  behind `azure-ai-evaluation` in the `[eval]` extra, off by default).
  `orchestrator_eval.py` runs each scenario through the whole graph and writes
  escalation records to a throwaway store. `run_all.py` (P4.5) is the consolidated
  deploy gate — subprocesses all three suites, one non-zero exit.
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
  `DefaultAzureCredential` bearer token (scope `https://ai.azure.com/.default`)
  and an injected W3C `traceparent` (`opentelemetry.propagate.inject`).
- **fake**: deterministic stubs for `tests/` (keyword classifier — a keyword hit
  scores ≥ 0.85 so the orchestrator routes it; fake resolver cites real `docs/`
  doc_ids).

The LangGraph graph topology is identical in all modes. Per-agent overrides:
`HELPDESK_CLASSIFIER_MODE`, `HELPDESK_RESOLVER_MODE` — the orchestrator builds a
`CompositeInvoker` (via `build_graph_invoker`) that honours each independently, so
the graph can run against the deployed agents while itself in-process.

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
  chosen for the small flat KB.
- **Resolver** (Phase 3): `gpt-5.4-mini` (fallback `gpt-5-mini` — manual
  `HELPDESK_RESOLVER_MODEL` override, the builder does no availability check) via
  `FoundryChatClient` on the project endpoint. Retrieval is a custom MAF **`@tool`**
  (`from agent_framework import tool` — `ai_function` is not exported)
  `search_knowledge_base` wrapping `KnowledgeBaseSearch.search()` (NOT the context
  provider, NOT the hosted search tool) so tool-call spans + explicit context feed
  the evaluators. `default_options` bakes `response_format` (a constraint-free
  `_ResolverDraft`, re-validated into `ResolverOutput` client-side) **and**
  `tool_choice="required"` (auto-resets after the first tool turn) — the Foundry
  host drops per-request options, so both must be agent defaults. Billing
  short-circuits to `policy_escalation` in every invoker before any model/search
  call; `resolve()` downgrades `answered`-without-citations to `not_grounded`.
- **Eval judge**: `claude-sonnet-5` or full `gpt-5`. NEVER a `*mini*` model as the
  judge (`eval/judges.py` raises if it sees `mini`). Groundedness score quality
  degrades with judge tier.

## Routing (orchestrator)

`classify → route → resolve | escalate_low_confidence → finalize`
(`src/helpdesk/agents/orchestrator/graph.py`). `route` is a pass-through node; the
branch is `add_conditional_edges`. `run_graph(graph, request, *, tracer=None)`
returns a `HelpdeskResult`.

- confidence < `Settings.confidence_threshold` (0.8, tuned in Phase 1) →
  `escalate_low_confidence` → reason `low_confidence`, regardless of category
  (resolver never called).
- billing is owned by the resolver seam: `invoke_resolver` short-circuits it to
  `policy_escalation` before any model or search call (README §4 — no resolution
  attempt), in every invoker (local/remote/fake). The graph does **not**
  special-case it.
- resolver not grounded → escalate reason `not_grounded`.

Escalation = an `EscalationRecord` written to the store in `finalize` — the
graph's single I/O point (JSON file).

## KB / search

Two indexes: `support-index`, `hr-index`, built from `docs/` frontmatter
`category` (`Settings.index_for`; billing has no index → `ValueError`). Chunked at
H2 sections (`src/helpdesk/search/chunking.py`, pure). **Push model** — embed
locally with `text-embedding-3-small` (1536-d, `Settings.embedding_model` /
`embedding_dimensions`) via the project OpenAI endpoint, `merge_or_upload` whole
docs with vectors; no indexer/skillset. Query = hybrid vector+keyword + semantic
ranker (`Settings.search_query_type` = `vector_semantic_hybrid` | `vector_hybrid`
| `keyword`). `KnowledgeBaseSearch.search(category, query)` (`search/client.py`)
is the resolver seam — Phase 3's `make_search_tool` wraps it in a MAF `@tool` that
returns JSON snippets; `SearchResult.to_citation()` → `contracts.Citation`.
Agentic / Knowledge Base mode is a flag (`Settings.agentic_search`, off by
default). Build: `uv run python scripts/build_kb.py --recreate`
(`--dry-run` chunks only, no network). Query: `uv run python scripts/search_kb.py
--category support --query "..."`.

## Common commands

Run scripts and modules through `uv run` (it resolves the project venv).

```
uv sync                                          # install (dev: uv sync --extra dev)
uv run pytest                                     # fast tests (hermetic)
uv run ruff check . && uv run mypy                # lint + types
az login  /  azd auth login                       # auth (DefaultAzureCredential)
uv run python scripts/run_local_classifier.py --message "..."
uv run python scripts/run_local_resolver.py --category support --message "..."
uv run python scripts/run_local_graph.py --message "..." [--mode local|remote|fake]
uv run python -m eval.orchestrator_eval --gate    # end-to-end routing matrix
uv run python scripts/build_kb.py --recreate      # build search indexes (--dry-run: chunk only)
uv run python scripts/search_kb.py --category support --query "vpn drops"   # manual KB query
uv run python -m eval.run_all                     # consolidated deploy gate (pre-deploy, live models)
uv run python -m eval.run_all --remote            # post-deploy: parity pass vs the local reports
uv run python scripts/verify_deploy.py <agent>    # post-deploy smoke test (classifier|resolver|orchestrator)
uv run python scripts/verify_trace_propagation.py # assert one correlated App Insights trace
```

## Deployment (azd, one agent at a time)

Unified `azure.yaml` model (one file, an `azure.ai.agent` service per agent — no
separate `manifest.yaml`/`agent.yaml`). Code-deploy mode, `remote_build`.

```
azd ext install azure.ai.agents                  # (+ azure.ai.projects)
azd env select helpdesk-dev
azd env set HELPDESK_CLASSIFIER_MODEL gpt-4.1-mini
uv run python -m helpdesk.agents.classifier.host  # raw local host :8088
azd ai agent run                                  # local host via startupCommand
azd ai agent monitor classifier --follow          # stream logs (needs a session id or --follow)
azd provision --preview  &&  azd provision        # connects to existing helpdesk-dev
azd deploy classifier                             # single service
uv run python scripts/verify_deploy.py classifier # post-deploy smoke test
```

**Resolver (Phase 3)** — same flow. Only two azd env vars are needed
(`azure.yaml` passes just these; everything else — embedding model, index names,
semantic config, query type — defaults correctly in `Settings`, and
`env_ignore_empty=True` means an unset `${VAR}` can't clobber a default):

```
azd env set HELPDESK_RESOLVER_MODEL gpt-5.4-mini   # or gpt-5-mini if 5.4 isn't enabled
azd env set HELPDESK_SEARCH_ENDPOINT https://srch-helpdesk-dev-isvx3zxptjnfy.search.windows.net
azd provision --preview  &&  azd provision        # registers helpdesk-resolver
azd deploy resolver
# grant on the resolver agent's managed identity (principalId from `azd env get-values`):
#   - "Search Index Data Reader"          on srch-helpdesk-dev-isvx3zxptjnfy
#   - "Cognitive Services OpenAI User"    at the cog-isvx3zxptjnfy account scope (embeddings /openai/v1)
uv run python scripts/verify_deploy.py resolver
# tail container logs while smoke-testing (needs a session — invoke once first):
azd ai agent invoke resolver $'request_id: s1\nCategory: support\nUser request:\nvpn drops'
azd ai agent monitor resolver --tail 300
HELPDESK_RESOLVER_MODE=remote uv run python -m eval.resolver_eval --gate \
  --min-routing-accuracy 0.85 --compare <local report> --min-parity 0.95
```

**Orchestrator (Phase 4)** — no `azd provision` (connect-to-existing project;
that CLAUDE.md line is boilerplate — there's no `infra/main.bicep`, `azd deploy
<service>` registers the agent directly). No model of its own; it calls the two
deployed agents (`HELPDESK_AGENT_MODE=remote`, endpoints as
`${AGENT_{CLASSIFIER,RESOLVER}_RESPONSES_ENDPOINT}` in `azure.yaml`). **No RBAC
grant** — implicit project access covers the sibling calls.

```
# 1. App Insights (once) — workspace-based component, connect it to the project
#    in the Foundry portal (project → Tracing → Connect Application Insights).
#    Then redeploy classifier + resolver so the runtime injects the conn string.
azd deploy classifier && azd deploy resolver
# 2. orchestrator
azd deploy orchestrator
azd env set HELPDESK_ORCHESTRATOR_AGENT_ENDPOINT $(azd env get-value AGENT_ORCHESTRATOR_RESPONSES_ENDPOINT)
uv run python scripts/verify_deploy.py orchestrator          # 5 canned end-to-end cases
uv run python scripts/verify_trace_propagation.py            # one correlated App Insights trace
# local host: uv run python -m helpdesk.agents.orchestrator.host
```

**Gotchas (orchestrator deploy):** (1) container root FS is **read-only** — the
JSON escalation store needs `HELPDESK_ESCALATION_STORE_PATH=/tmp/…` (set in
`azure.yaml`); without it every *escalated* request 500'd. (2) `OTEL_SERVICE_NAME=
helpdesk-orchestrator` in `azure.yaml` — else its graph-node spans show as
`cloud_RoleName="unknown_service"`. (3) 2 CPU / 4 Gi — the langgraph +
langchain-azure-ai + azure-monitor stack is heavy.

**Gotcha (first resolver deploy):** `azure.yaml` env values are `${VAR}`
substitutions from the azd env; an unset one expands to `""`. Before
`env_ignore_empty`, that made `HELPDESK_EMBEDDING_MODEL=""` → the container's
embeddings call went out with `model=""` → 404 `DeploymentNotFound` → every
support/hr request escalated `not_grounded`. Fix was `env_ignore_empty=True` +
trimming `azure.yaml` to only the vars that lack a default.

Order: classifier, then resolver, then orchestrator. Verify each before the next.
Your account is Owner + Foundry User; the deployed agent's MI has implicit
project-endpoint inference access, so with `FoundryChatClient` (project endpoint)
no RBAC grant is needed. Runtime injects `FOUNDRY_PROJECT_ENDPOINT` and
`APPLICATIONINSIGHTS_CONNECTION_STRING` (un-prefixed
— `Settings` bridges them via `AliasChoices`); pass everything else through
`azure.yaml` `env:`.

## Tracing

`src/helpdesk/tracing.py`: `build_tracer(settings)` returns an
`AzureAIOpenTelemetryTracer` (from `langchain-azure-ai`), or `None` when
`HELPDESK_TRACING_ENABLED=false`. `run_graph` attaches it as a callback
(`config={"callbacks": [tracer]}`) and sets `helpdesk.request_id` OTel baggage.
With an App Insights connection string set the tracer auto-configures Azure
Monitor export; without one it still emits spans on a local `TracerProvider` that
`configure_tracing` installs (enough for `trace=` in logs + `traceparent` on the
`RemoteInvoker` calls). App Insights `appi-helpdesk-dev` is connected to the
project, so the runtime injects `APPLICATIONINSIGHTS_CONNECTION_STRING` into all
three agent containers. **Trace-context propagation across the orchestrator→agent
calls is VERIFIED** (`scripts/verify_trace_propagation.py`): one `operation_Id`
spans the orchestrator's node spans + `POST .../helpdesk-{classifier,resolver}/…`
dependencies, the classifier's `chat gpt-4.1-mini`, and the resolver's `chat
gpt-5.4-mini` + `execute_tool search_knowledge_base`. `verify_trace_propagation.py`
stands up its own `TracerProvider` + Azure Monitor exporter (the verify process
isn't a graph host), fires one request in a recording root span, and polls
`requests`/`dependencies` via `azure-monitor-query` until all three roles appear.
View: Azure Monitor → Investigate → Agents (Preview) for the graph; Foundry portal
→ Observability → Traces for agent spans.

## Do not

- Do not commit real secrets. `.env` is gitignored; `.env.example` is the template.
- Do not treat `docs/` as real policy.
- Do not use a mini-tier model as an eval judge.
- Do not add business logic to `scripts/`.
