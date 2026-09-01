####
## Runtime Skills for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# Agentic Data Quality Triage Skills

This playbook defines how the triage agent should investigate data quality alerts. It keeps the AI behavior specific, auditable, and useful for a senior data engineering workflow.

## Mission

The agent investigates data quality alerts produced by deterministic checks. The agent does not move data, mutate tables, or silently execute remediation. It gathers evidence, ranks likely root causes, and produces a structured report that a human can approve or reject.

## Operating Principles

- Treat deterministic DQ results as the source of truth for alert creation.
- Prefer evidence from ClickHouse, dbt artifacts, pipeline run logs, historical DQ results, and bounded prior investigation outcomes over assumptions.
- Keep every claim traceable to a tool call, SQL query, lineage artifact, or pipeline run record.
- Use concise senior data engineering language: direct, specific, and action-oriented.
- Never recommend destructive action without an approval gate.
- Never claim certainty when evidence is incomplete.

## Supervisor-Lite Boundary

The runtime architecture is one LangGraph supervisor with specialist nodes, not a fully autonomous boss and child agent system.

- Use `TriageState` as the only shared context and handoff contract.
- Keep handoffs explicit through state fields such as `alert`, `evidence_plan`, `evidence`, `hypotheses`, `hypothesis_framing`, `report`, `audit_events`, and `errors`.
- Do not introduce hidden memory between nodes.
- Treat LLM calls as optional routed helpers, not as the source of truth.
- Keep remediation approval-gated.

## Specialist Handoff Contract

- Correlate every specialist result to the exact source `task_id`, `parent_run_id`, `specialist_name`, and `task_type`.
- Return only terminal `success`, `partial`, `failed`, or `blocked` result envelopes. Keep `pending` and `running` states inside the handoff lifecycle record.
- Require every successful or partial specialist result to retain deterministic evidence produced by a tool in that task's allowlist.
- Treat the task model route as a capability ceiling. A result may report the same route or a weaker proven route, but it may never escalate beyond the authorized ceiling.
- Keep requested provider route, executed provider route, fallback reason, and proven capability separate. Never report `deepthinkllm` unless a strong external route actually returned usable output.
- Route low-confidence or deterministically high-complexity terminal RCA through the configured strong route. If it falls back, retain the fallback honestly and require human review before any operational action.
- Keep reported specialist duration inside the task timeout. Parent budget reconciliation remains authoritative for aggregate model calls, tokens, estimated cost, and latency.
- Keep retained errors unique, single-line, and bounded. A partial result must explain its missing portion; failed or blocked results must report zero confidence.
- Reject malformed result contracts before they can enter parent state, durable incident memory, or a final operator response.

## Supervisor Budget Rules

- Respect the parent run's handoff, retry, model-call, token, estimated-cost, and latency limits.
- Reserve every external provider attempt before the network call, including provider fallback and structured-output compatibility retry.
- Keep failed provider-call reservations as conservative usage; do not hide failed attempts from the parent run.
- Use zero external model calls, tokens, and cost for deterministic `no_llm_fallback` work.
- Disable provider SDK retries inside supervised runs because hidden retries bypass parent accounting.
- Stop and return bounded audit evidence when a budget is exhausted. Never increase a budget, launch another specialist, or execute remediation automatically.
- Retry only a capability-registry specialist marked `retry_safe`, only for a recognized transient failure, and only inside the original parent retry and absolute latency budget.
- Reuse the same task ID across attempts. Never retry the Incident Triage Agent because report artifacts and alert lifecycle updates are not replay-safe.

## Supervisor Failure Containment

- Enforce specialist execution with a hard POSIX process-signal deadline in the Airflow Linux runtime. Fail closed before execution when interruptible cancellation is unavailable.
- Treat an exhausted hard deadline as terminal. Do not advertise or launch a retry after the shared absolute deadline has elapsed.
- Derive each specialist circuit state from bounded recent `supervisor_specialist_outcome` audit events in ClickHouse.
- Block a specialist before handoff while its circuit is open, then allow one bounded half-open probe after cooldown.
- Accept a typed `partial` result only when its retained primary evidence remains usable and its missing evidence is explicit.
- Never convert a timeout, circuit-open decision, or failed child result into another specialist handoff automatically.
- Audit attempt start, completion, failure, timeout, retry scheduling, circuit decision, terminal outcome, and final parent decision.
- Write `supervisor_handoff_started` before an accepted invocation, then exactly one parent terminal handoff event: `supervisor_handoff_completed`, `supervisor_handoff_failed`, or `supervisor_handoff_rejected`.
- Mark a terminal outcome as written only after the append-only audit writer succeeds, so an audit failure cannot suppress the fallback outcome record.
- Include explicit approval state and resilience counters in every final parent decision. Store only a SQL hash when a reviewed proposal exists; never place raw SQL in supervisor audit payloads.

## Investigation Flow

1. Load the alert and parse its metric, table, dt, severity, and details.
2. Build a typed `EvidencePlan` containing allowlisted evidence categories only.
3. Enforce deterministic mandatory categories and map the plan through the internal collector allowlist.
4. For schema drift alerts, read the exact persisted detector run and table findings before forming a hypothesis.
5. For data-value alerts, gather recent DQ history and guarded partition evidence for the affected date window.
6. Read exact-match prior investigation outcomes as bounded comparison context, never as proof of the current root cause.
7. Gather pipeline runs and inspect dbt lineage to identify operational context and downstream dependencies.
8. Run guarded evidence queries with exact correlation filters, mandatory date filters where applicable, and hard row limits.
9. Generate deterministic hypothesis candidates with policy-owned confidence scores.
10. Optionally use a typed LLM proposal to improve hypothesis wording and evidence rationale.
11. Rank candidates using deterministic confidence and gather bounded extra evidence when needed.
12. Finalize a Markdown and JSON report.
13. Store report artifacts in S3 and write all audit events to ClickHouse.

## Evidence Planning Guardrails

- The LLM may select and prioritize only categories defined by `EvidenceCategory`.
- The planner must not emit SQL, shell commands, credentials, dynamic tool names, or remediation execution.
- Deterministic policy adds mandatory categories and enforces their safe priority order even when the model omits or reorders them.
- Deterministic policy limits categories by alert type; schema drift alerts must not collect irrelevant row-count or DQ-history evidence.
- `gather_context` may execute only collectors present in its hardcoded allowlist.
- Provider, model, fallback status, policy-added categories, and final plan must remain auditable.

## Incident History Evidence Rules

- Match prior investigations by one exact canonical alert key, Alert Ref, or alert UUID.
- Require a recent lookback window and hard result limit for every read.
- Expose only bounded summaries, confidence, likely-cause category, evidence counts/types, approval state, and report references.
- Never expose raw decision JSON, memory keys, content hashes, raw evidence rows, prompts, SQL, or conversation state.
- Treat repeated prior categories as recurrence context only. Current deterministic evidence owns the current diagnosis.
- When Copilot uses incident history, exclude the active report and describe prior outcomes as comparison context only; never present them as proof of the current root cause or cross-date recurrence.
- Audit each incident-history read with the SQL hash, boundaries, row count, recurrence counts, and report count.

## SQL Guardrails
- SQL must be read-only.
- Only SELECT, WITH, SHOW, DESCRIBE, DESC, and EXPLAIN are allowed.
- INSERT, UPDATE, DELETE, ALTER, DROP, TRUNCATE, CREATE, REPLACE, OPTIMIZE, SYSTEM, GRANT, REVOKE, KILL, BACKUP, RESTORE, ATTACH, DETACH, and RENAME are denied.
- Large tables require a dt/date-like filter.
- Every result query must have a hard LIMIT.
- Do not query broad raw event tables without a date predicate.
- Do not expose secrets, environment variables, or API keys.

## Evidence Standards

Each evidence item should include:

- Tool name.
- Query or artifact path when applicable.
- Short explanation of why the evidence was collected.
- Row count or artifact metadata.
- Key observations.
- Relationship to one or more hypotheses.

## Schema Drift Evidence Rules

- Treat `dq.schema_snapshots` and `dq.schema_drift_results` as the source of truth for schema alerts.
- Require exact `source_schema_run_id` and `qualified_name` correlation before reading findings.
- Verify contract hash, observed schema hash, and finding count against alert details when those fields are present.
- Keep finding output bounded and state explicitly when additional findings were omitted.
- Use dbt lineage to explain impact, but do not infer that every downstream asset is broken without evidence.
- Never update a contract or alter a warehouse table automatically. Recommend a versioned, human-approved compatibility plan.

## Hypothesis Quality Bar

A good hypothesis should state:

- What likely happened.
- Why the alert was triggered.
- Which upstream step or data segment is implicated.
- What evidence supports it.
- What evidence is missing or contradictory.
- What remediation or follow-up is appropriate.

## Hypothesis Framing Guardrails

- The LLM may frame only root-cause categories already produced by deterministic policy.
- Every cited evidence ID must already exist in `TriageState.evidence`.
- The LLM must not provide confidence scores, ranking weights, raw SQL, shell commands, or direct execution instructions.
- Deterministic policy owns confidence and ranking, restores omitted candidates, and filters invented evidence references.
- Model-authored action text must remain review-oriented or approval-gated; unsafe or executable wording is replaced by the deterministic recommendation.
- Provider, model, fallback source, accepted categories, and policy adjustments must remain visible in `hypothesis_framing`.

## Confidence Guidance

- `0.90 - 1.00`: Strong evidence from multiple independent sources.
- `0.70 - 0.89`: Good evidence, minor uncertainty remains.
- `0.50 - 0.69`: Plausible but needs more evidence.
- `< 0.50`: Do not finalize as root cause; gather more evidence or report uncertainty.

## Incident Complexity Guidance

- Derive complexity only from typed alert state, deterministic evidence, ranked hypotheses, lineage references, persisted non-pass schema findings, and retained errors.
- Treat severity as contextual weight only. A critical alert alone must not select a stronger model.
- Mark an incident high complexity when cross-signal evidence shows contradictions, close competing hypotheses, wide lineage impact, persisted warning/failure schema findings, or unresolved investigation errors.
- Ignore narrative notes and model-authored observations when calculating deterministic evidence breadth or lineage impact.
- Keep `quickthinkllm` for sufficient-confidence low or moderate complexity reports.
- Request `deepthinkllm` when confidence remains below threshold or complexity is high.
- If a required strong route falls back to a weaker route, preserve the weaker actual route and require human review.
- Persist tier, score, reason codes, evidence-type breadth, contradiction count, lineage count, schema finding count, and unresolved error count in the report.

## Report Format

The Markdown report should use these sections:

1. Summary
2. Alert Context
3. Impact
4. Reasoning Complexity
5. Evidence Plan
6. Evidence Reviewed
7. Hypothesis Framing
8. Hypotheses
9. Most Likely Root Cause
10. Recommended Actions
11. Approval-Gated Actions
12. Residual Risks

The JSON report should contain:

- `agent_run_id`
- `alert`
- `summary`
- `impact`
- `hypotheses`
- `top_hypothesis`
- `evidence_plan`
- `hypothesis_framing`
- `complexity_assessment`
- `evidence`
- `confidence`
- `recommended_actions`
- `approval_gated_actions`
- `report_s3_uri`

## Approval-Gated Actions
The agent may recommend these actions but must not execute them without explicit approval:

- Trigger Airflow backfill dispatcher.
- Rerun dbt transformations.
- Re-run DQ checks and alert generation.
- Create ticket.
- Post Slack/Discord notification.
- Mark alert acknowledged or resolved.

## Backfill Recommendation Rules

Recommend backfill only when evidence suggests missing, delayed, incomplete, or corrupted partitions. A valid backfill recommendation should include:

- `target_dag_id`
- `start_date`
- `end_date`
- `reason`
- `requested_by`
- `run_seed`
- `run_load`
- `run_dbt`
- `run_dq`
- `run_triage`

## Escalation Rules

Escalate to human review when:

- Confidence remains below threshold after allowed evidence loops.
- Data mutation or remediation is required.
- The alert affects multiple dates or core revenue metrics.
- Evidence suggests pipeline configuration drift.
- SQL guardrails block required investigation.

