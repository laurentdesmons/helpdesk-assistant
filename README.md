# IT Help Desk Agent Assistant — Design Doc

*Living document, updated each phase. Current: Phase 4 (part 1) — **LangGraph
orchestrator built and verified locally, not yet deployed.**
`src/helpdesk/agents/orchestrator/` = `graph.py` (`classify → route → resolve |
escalate_low_confidence → finalize`, pure `build_graph(settings, invoker=…)` +
`run_graph`) + `host.py` (a `SupportsAgentRun` shim over the compiled graph for
`ResponsesHostServer`). `src/helpdesk/tracing.py` = `AzureAIOpenTelemetryTracer`
factory + OTel-SDK setup; `RemoteInvoker` now injects W3C `traceparent` on the
agent calls. `agent_gateway.CompositeInvoker` / `build_graph_invoker` let the
classifier and resolver run at different modes in one graph run.
`scripts/run_local_graph.py`, `eval/orchestrator_eval.py` (code-based) + a 14-row
scenario set, `orchestrator` service in `azure.yaml` (written, not `azd`-deployed).
**100% outcome accuracy on the scenario set (fake + in-process); local↔remote
parity and App Insights trace-propagation verification land with the deploy
(part 2).** Previous: Phase 3 — `helpdesk-resolver` deployed and verified. Next:
Phase 4 part 2 (deploy orchestrator #3 + `verify_trace_propagation.py`), then
Phase 4.5 (`eval/run_all.py` gate).*

## 1. Category taxonomy
- `billing`
- `support`
- `hr`

## 2. Architecture

```
User request
    │
    ▼
LangGraph orchestrator (hosted agent in Foundry)
    │
    ├── node: classify  ──calls──▶  Classifier Agent (Foundry-hosted, Microsoft Agent Framework) [direct endpoint call]
    │
    ├── node: route (conditional edge on category)
    │
    └── node: resolve   ──calls──▶  Resolver Agent (Foundry-hosted, Microsoft Agent Framework) [direct endpoint call]
```

- Classifier and Resolver are **two independently deployed Foundry-hosted agents**, each built with Microsoft Agent Framework.
- LangGraph is the orchestrator, itself packaged as a hosted agent in Foundry, calling both directly via each agent's own Responses/Invocations protocol endpoint — plain HTTP call, no A2A. Reasoning: A2A on Foundry is preview-only and Microsoft explicitly doesn't recommend it for production; we don't need its discovery/negotiation layer since LangGraph already knows exactly which agent to call. Auth via managed identity (standard Foundry-to-Foundry call).
- Escalation = **flagged state**, no ticketing system integration — a human picks the request up from a queue/UI (mechanism TBD, Phase 5).
- Tracing: `langchain-azure-ai`'s `AzureAIOpenTelemetryTracer` attached to the LangGraph app, emitting per-node/edge OTel spans to Application Insights, visible in Foundry Observability > Traces. Each Foundry-hosted agent (Classifier, Resolver) also emits its own spans natively. Trace context propagation across the direct endpoint calls (so all three show up correlated under one trace) needs explicit verification in Phase 4 — not assumed.

**Phase 4 (part 1) build.** `src/helpdesk/agents/orchestrator/graph.py` is a pure
`build_graph(settings, *, invoker)` compiling the `StateGraph`
(`classify → route → resolve | escalate_low_confidence → finalize`) plus
`run_graph(graph, request, *, tracer=None)`; the caller owns the invoker's
lifetime (mirrors `build_resolver_agent(settings, kb=…)`). `route` is a pass-through
node; the conditional edge sends `confidence < confidence_threshold` (0.8) to
`escalate_low_confidence` regardless of category, everything else to `resolve`.
Billing is *not* special-cased in the graph — `agent_gateway.invoke_resolver`
short-circuits it to `policy_escalation` (§4). `finalize` is the graph's single
I/O point: it assembles the top-level `HelpdeskResult` (`contracts.py`) and, on
any escalation, writes an `EscalationRecord` to the store (§6). `host.py` wraps
the compiled graph in a minimal `SupportsAgentRun` shim (`_GraphAgent`) so the
existing `ResponsesHostServer` can host it — same deploy zip, `main.py` selects on
`HELPDESK_AGENT_ROLE=orchestrator`. `src/helpdesk/tracing.py` builds the tracer
(no-op export when no App Insights connection string; still emits spans locally)
and installs an OTel-SDK `TracerProvider` so `RemoteInvoker`'s
`opentelemetry.propagate.inject` produces a real `traceparent` on the
classifier/resolver calls. `agent_gateway.CompositeInvoker` +
`build_graph_invoker(settings, mode=None)` let each agent follow its own
`HELPDESK_{CLASSIFIER,RESOLVER}_MODE` in one graph run — how the graph is
exercised against the deployed agents before the orchestrator itself deploys.
`scripts/run_local_graph.py` drives it; `eval/orchestrator_eval.py` +
`eval/datasets/orchestrator_scenarios.jsonl` (14 rows, all 5 outcomes) gate
outcome accuracy, a billing sub-gate, low_confidence recall, citation validity,
and that every escalation wrote a record. Deploy + `verify_trace_propagation.py`
are part 2.

## 3. Classifier Agent — contract

**Input**
```json
{ "request_id": "string", "user_id": "string", "message_text": "string", "channel": "string" }
```

**Output**
```json
{ "category": "billing | support | hr", "confidence": 0.0, "rationale": "string" }
```

**Confirmed:** below-threshold confidence → route to `escalate_human` regardless of category, rather than guessing.

**Phase 1 result (`gpt-4.1-mini`):** `confidence_threshold = 0.80`. On the 49-row synthetic labeled set the classifier scores **100%** accuracy, in-process and through the deployed endpoint (100% local↔remote category parity). All correct predictions land ≥ 0.70; the one genuinely ambiguous procurement request (`s14`, "licensed copy of… software approved for my team") is classified correctly at 0.70 and so escalates at threshold 0.80 — the intended behaviour. Both `ambiguous`-flagged rows fall below 0.80. Re-tune against real request samples when available. *(The earlier Haiku build scored 98% at the same threshold; swapped for the reasons below.)*

**Confirmed:** Model = **`gpt-4.1-mini`** (Foundry model catalog, portal-managed), called through Microsoft Agent Framework's **`FoundryChatClient`** against the **project endpoint** (OpenAI-family Responses API). *Was* Claude Haiku 4.5 via `AnthropicFoundryClient`; switched after the first deploy — the prerelease `agent-framework-anthropic` + Anthropic-on-Foundry path threw frequent transient `server_error`s in the hosted container, and calling the account-level Anthropic endpoint needed an extra account-scope RBAC grant on the agent identity. `FoundryChatClient` on the project endpoint runs under the agent MI's *implicit* inference access (no grant), shares one client stack with the resolver, and drops a prerelease dep. Structured output (`Classification`) via `response_format`; the JSON schema stays constraint-free (strict structured-output validators — Claude and OpenAI alike — reject numeric `minimum`/`maximum`), so bounds are enforced in a Pydantic validator. Framework stays Microsoft Agent Framework.

**Deployment (Phase 1 deploy).** The classifier ships as a Foundry **Hosted Agent** — `src/helpdesk/agents/classifier/agent.py`'s builder wrapped by `agent-framework-foundry-hosting`'s **`ResponsesHostServer`** (`POST /responses` + `GET /readiness`, port 8088), started via a root `main.py` shim. `azd` code-deploy mode (`azure.yaml` `classifier` service, `dependencyResolution: remote_build`), no Dockerfile. `response_format=Classification` is baked into the agent's `default_options` so structured output holds when the host calls `agent.run()` directly. The orchestrator (and `scripts/verify_deploy.py`) reach it through `agent_gateway.RemoteInvoker`: async httpx POST `{"input": <prompt>, "stream": false}` to `.../agents/helpdesk-classifier/endpoint/protocols/openai/responses`, bearer token scope `https://ai.azure.com/.default`, with JSON-parse + retry-with-backoff (transient `status: failed` and unparseable responses both consume an attempt). App Insights / trace-context propagation is deferred to Phase 4.

## 4. Resolver Agent — contract

**Input**
```json
{ "request_id": "string", "category": "billing | support | hr", "message_text": "string" }
```

**Behavior by category**
| Category | Behavior |
|---|---|
| `billing` | Escalate to human (flag state). No resolution attempt. |
| `support` | RAG against Azure AI Search index (policy + KB docs). If grounded → answer. If not grounded → escalate to IT support (flag state). |
| `hr` | Same as `support`: RAG against HR policy/KB docs. If grounded → answer. If not grounded → escalate to human (flag state). |

**Confirmed:** separate indexes — `support-index` and `hr-index`. Keeps permissioning and content lifecycle independent.

**Confirmed:** Model = GPT-5.4-mini (dev only), via Foundry model catalog + Microsoft Agent Framework's `FoundryChatClient`. Fallback: gpt-5-mini if 5.4 isn't enabled on the subscription. To be re-evaluated against Sonnet 5 / full GPT-5 once real eval results are in (see Section 7) — mini-tier carries real accuracy risk on the groundedness judgment specifically.

**Output shape (`ResolverOutput`, folded in from `contracts.py`):**
```json
{
  "request_id": "string",
  "status": "answered | escalated",
  "answer": "string | null",              // required iff status == "answered"
  "citations": [ { "doc_id": "string", "title": "string?", "snippet": "string?", "score": 0.0 } ],
  "escalation_reason": "not_grounded | policy_escalation | low_confidence | null"  // required iff escalated
}
```

**Phase 3 build.** `src/helpdesk/agents/resolver/agent.py` mirrors the classifier:
a pure `build_resolver_agent(settings)` on `FoundryChatClient` (project endpoint,
`HELPDESK_RESOLVER_MODEL`), a `resolve(agent, req)` run helper, `instructions.md`.
Retrieval is `make_search_tool(settings)` — a MAF **`@tool`** (`from agent_framework
import tool`; `ai_function` isn't exported) named `search_knowledge_base` that
wraps `KnowledgeBaseSearch.search(category, query)` and returns JSON snippets
(`doc_id`, `title`, `snippet`, `score`). *Not* the `agent-framework-azure-ai-search`
context provider and *not* the hosted search tool — an explicit tool so tool-call
spans + retrieved context feed the evaluators. `default_options` bakes in
`response_format` **and** `tool_choice="required"` (which `agent_framework`
auto-resets to `auto` after the first tool turn), because the Foundry host server
drops per-request options — so one forced grounded retrieval has to be the agent
default. Structured output goes through a constraint-free `_ResolverDraft` model
(strict JSON-schema validators reject `min_length`, exactly as for
`Classification`), re-validated into `ResolverOutput` client-side.

**Confirmed behaviour:**
- **billing** — `invoke_resolver` short-circuits to
  `status="escalated"`, `escalation_reason="policy_escalation"` **before any model
  or search call**, in every invoker (local / remote / fake). One place owns
  billing.
- **support / hr** — the model must call `search_knowledge_base`, answer only from
  the returned snippets with ≥1 citation, else escalate `not_grounded`.
- **guard** — `resolve()` downgrades an `answered` result with empty `citations`
  to `escalated` / `not_grounded`; two unparseable attempts also fall back to
  `not_grounded` rather than failing the request.

**Deploy (Phase 3 deploy).** Ships as a Foundry **Hosted Agent** alongside the
classifier — both services deploy the same zip; `main.py` selects the host on
`HELPDESK_AGENT_ROLE`. `resolver` service in `azure.yaml` (code-deploy,
`remote_build`, CPU 1 / 2Gi for the search + embeddings client stack). The
resolver MI needs two explicit grants its implicit inference access doesn't
cover: `Search Index Data Reader` on the Search service and `Cognitive Services
OpenAI User` at the **account** scope (the query-embedding call hits the
account-level `/openai/v1` route). `Settings` uses `env_ignore_empty=True` so an
unresolved `${VAR}` from `azure.yaml` falls back to its default instead of
clobbering it with `""`. Verified: `verify_deploy.py resolver` all-pass, remote
eval 100%, **100% local↔remote routing parity (35/35)**; hosted latency ~15–24s.

## 7. Evaluation plan

**Classifier** — code-based evaluator, no LLM judge. Labeled test set (request text → known correct category) → accuracy/precision/recall/confusion matrix per category (billing/support/hr).

**Resolver** — Phase 3 ships a **code-based** `eval/resolver_eval.py` (no LLM
judge): routing accuracy (answer vs. escalate + the escalation reason), a hard
billing sub-gate (every billing row must escalate `policy_escalation`),
not-grounded precision/recall over the off-KB rows, and citation validity (every
cited `doc_id` must exist in the KB — derived from `docs/` via the pure
`iter_chunks`). 35-row labeled set at `eval/datasets/resolver_labeled.jsonl`;
gpt-5.4-mini scores 100% on every metric locally.

The LLM-judge evaluators below are **scaffolded but not run** — `--judge` wires
`azure-ai-evaluation` (in the `[eval]` extra) if installed, and a real
groundedness score still needs a labeled set against the *actual* KB (see the
blocking gap). Foundry built-in RAG/agent evaluators, for when that lands:
- Groundedness (response supported by retrieved context, not fabricated)
- Relevance (response addresses the query)
- Retrieval Quality (isolates retrieval failures from generation failures)
- Tool Call Accuracy / Tool Output Utilization (resolver calls Azure AI Search as a tool)

**Judge model — do not use mini-tier.** Microsoft's own internal evaluator-quality study found Groundedness has a real score-quality gap by judge tier and that smaller judges produce worse scores, not a fixable problem, unlike the other evaluators tested. Use Sonnet 5 or full GPT-5 as the judge, independent of what model the resolver itself runs on.

**Workflow:** local fast-iteration evals via Microsoft Agent Framework's `EvaluateAsync` during dev → Foundry cloud evaluators as pre-deployment gate → production monitoring post-deploy via Azure Monitor with quality-threshold alerting.

**Blocking gap:** a real groundedness eval needs a labeled test set against the *actual* KB, not the placeholder sample docs — those only validate pipeline mechanics.

## 5. KB / grounding design (Azure AI Search)

**Phase 2 build (`src/helpdesk/search/`).** A custom, push-model retrieval layer —
*not* the `agent-framework-azure-ai-search` context provider and not the hosted
search tool (the Phase 3 resolver wraps `KnowledgeBaseSearch.search()` in its own
`@ai_function` so tool-call spans + explicit context feed the evaluators).

- **Chunking** (`chunking.py`, pure): each `docs/*.md` file → one chunk per `## `
  section, frontmatter `category` picks the index. Placeholder corpus = 6 docs ×
  4 sections = **24 chunks** (12 support + 12 hr). Sections are short and
  self-contained, so H2 is the natural retrieval unit.
- **Two indexes** — `support-index`, `hr-index` (`Settings.index_for()`; `billing`
  has none, it escalates). Independent permissioning + content lifecycle.
- **Embeddings** (`embeddings.py`): `text-embedding-3-small` (1536-d) via the
  **account-level** `https://<acct>.services.ai.azure.com/openai/v1` endpoint —
  the project-scoped `.../api/projects/<proj>/openai/v1` route proxies
  chat/responses but not `/embeddings`. `-small` is ample for a small, flat,
  lexically-distinct KB where the hybrid + semantic ranker carries retrieval;
  **re-evaluate `text-embedding-3-large` when the real (larger, multi-section,
  possibly multi-hop) KB replaces the samples.**
- **Index build** (`pipeline.py` + `scripts/build_kb.py`): chunk → embed locally →
  `merge_or_upload` whole documents (vectors included). No indexer, no skillset —
  the corpus embeds in <1s and we want per-chunk logs + `--dry-run`. **Revisit
  integrated vectorization + a scheduled indexer when the real KB is large and
  frequently updated.**
- **Query** (`client.py`): HNSW/cosine vector arm + BM25 keyword arm + semantic
  ranker (`Settings.search_query_type` = `vector_semantic_hybrid` default;
  `vector_hybrid` / `keyword` for eval ablation). Returns `SearchResult`
  (→ `contracts.Citation` via `.to_citation()`).
- **Agentic / Knowledge-Base mode** — still just the `Settings.agentic_search`
  flag; multi-hop reasoning over Knowledge Bases lands if/when the real KB needs
  multi-document synthesis.
- **Provisioning** — manual (consistent with portal-managed models): Azure AI
  Search **Basic** tier, semantic ranker on the **free** plan, AAD auth
  (`DefaultAzureCredential`); `text-embedding-3-small` deployed via the portal
  Model catalog; `Search Service Contributor` + `Search Index Data Contributor`
  for the dev user; `HELPDESK_SEARCH_ENDPOINT` in `.env`. **Phase 3 deploy adds,
  on the resolver agent's managed identity:** `Search Index Data Reader` on the
  Search service, a Foundry connection to it, **and** `Cognitive Services OpenAI
  User` at the *account* scope — the resolver's search tool embeds the query on
  the account-level `/openai/v1` route, which the MI's implicit (project-scoped)
  inference access does not cover.

**Sample content:** placeholder KB docs (`docs/`) — fictional, generic, marked
`status: SAMPLE PLACEHOLDER`. Not real policy. Swapped for actual documents
before production; those only validate pipeline mechanics.

**Still open — needed before real indexing:**
- Real policy/KB documents: format (PDF, Word, HTML, Confluence export)?
- Approximate volume (# of docs, size) — affects chunking strategy, `-small` vs
  `-large`, and semantic vs. agentic mode.
- Update frequency — push rebuild vs. integrated vectorization + scheduled indexer.

## 6. Escalation state schema (draft)

```json
{
  "request_id": "string",
  "category": "billing | support | hr",
  "escalation_reason": "not_grounded | policy_escalation | low_confidence",
  "status": "flagged",
  "created_at": "iso8601"
}
```

Where this queue lives (Foundry-native construct vs. external store + custom UI) — Phase 5, not blocking now.
