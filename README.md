# IT Help Desk Agent Assistant — Design Doc

*Living document, updated each phase. Current: Phase 1 (classifier) — built and
evaluated locally; deploy pending.*

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

**Phase 1 result:** `confidence_threshold = 0.80`. On a 49-row synthetic labeled set the classifier scored 98% accuracy; every correct prediction had confidence ≥ 0.85, the single misclassification 0.75, and deliberately vague requests < 0.6 — a clean separation at 0.80. Re-tune against real request samples when available.

**Confirmed:** Model = Claude Haiku 4.5, deployed via the Foundry model catalog. Called through Microsoft Agent Framework's **`AnthropicFoundryClient`** (`agent-framework-anthropic`) — *not* `FoundryChatClient`, which targets the Azure-OpenAI-family Responses endpoint. Structured output (`Classification`) works natively via `options={"response_format": ...}`; the JSON schema must be constraint-free (Claude's structured-output validator rejects numeric `minimum`/`maximum`), so bounds are enforced in a Pydantic validator instead. Framework stays Microsoft Agent Framework (not Claude Agent SDK).

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

Two supported modes via the `agent-framework-azure-ai-search` context provider (currently pre-release/`--pre`):
- **Semantic mode**: hybrid (vector + keyword) search with semantic ranking. Simpler, GA-track, good fit if policy docs are relatively flat/short.
- **Agentic mode**: multi-hop reasoning over Knowledge Bases for complex queries. Higher latency/cost, better for multi-document synthesis.

**Sample content:** placeholder KB docs provided (`sample-kb/support/`, `sample-kb/hr/`) — fictional, generic content for Phase 1 build/test only. Not real company policy. Must be swapped for actual documents before production.

**Still open — needed before real indexing (Phase 2/3):**
- Real policy/KB documents: format (PDF, Word, HTML, Confluence export)?
- Approximate volume (# of docs, size) — affects chunking strategy and semantic vs. agentic mode choice.
- Update frequency — affects whether we need a re-indexing pipeline or one-time load.

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
