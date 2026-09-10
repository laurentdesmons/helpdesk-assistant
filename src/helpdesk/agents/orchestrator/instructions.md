# Help Desk Orchestrator

Routing brain for the IT help desk. Not a chat agent — it runs a fixed graph:

1. **classify** — call the classifier agent to get `{category, confidence, rationale}`.
2. **route** — if `confidence < confidence_threshold`, escalate to a human with
   reason `low_confidence`, whatever the category. Otherwise continue.
3. **resolve** — call the resolver agent. `billing` is escalated
   (`policy_escalation`) by the resolver seam before any model call; `support` /
   `hr` are answered from the knowledge base or escalated `not_grounded`.
4. **finalize** — return a `HelpdeskResult`. Every escalation is also written to
   the escalation store as an `EscalationRecord`.

Input: `{request_id, user_id, message_text, channel}`.
Output: `HelpdeskResult` — `{request_id, outcome, category?, answer?, citations?,
escalation_reason?, classification?}`.
