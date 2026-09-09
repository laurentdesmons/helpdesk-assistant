# IT Help Desk Agent Assistant — Design Doc

*Living document, updated each phase. Current: Phase 2 complete — KB / Azure AI
Search layer built and **verified end-to-end** (`src/helpdesk/search/`,
`scripts/build_kb.py`, `scripts/search_kb.py`, hermetic chunking tests).
Chunk-at-H2 → 24 chunks, two push-model indexes (`support-index`, `hr-index`),
`text-embedding-3-small` (1536-d), hybrid + semantic ranker. Search service
provisioned manually; `build_kb.py --recreate` populated both indexes and all
spot queries land on the right doc/section. Previous: Phase 1 — classifier
(`gpt-4.1-mini` on `FoundryChatClient`) **deployed** and verified 100% both ways.
Next: Phase 3 (resolver).*

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

## 7. Evaluation plan

**Classifier** — code-based evaluator, no LLM judge. Labeled test set (request text → known correct category) → accuracy/precision/recall/confusion matrix per category (billing/support/hr).

**Resolver** — Foundry built-in RAG/agent evaluators:
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
  for the dev user; `HELPDESK_SEARCH_ENDPOINT` in `.env`. Phase 3 adds a Foundry
  connection + `Search Index Data Reader` on the resolver agent MI.

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
