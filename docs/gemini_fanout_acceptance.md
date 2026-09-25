<!--
Gemini Evidence Fan-Out Acceptance
Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-->

# Gemini Evidence Fan-Out Acceptance

## Current Verdict

The three-purpose worker integration is implemented and passed deterministic
Airflow acceptance. Paid three-worker Gemini acceptance is **not complete**.
The 2026-09-11 attempt failed with `APIConnectionError` in all three workers.
A subsequent unauthenticated connectivity probe inside `dq_runner` failed during
DNS resolution of `generativelanguage.googleapis.com` with `socket.gaierror`.
This is evidence of a connectivity problem at test time, not proof of a billing
or quota rejection. Do not change payment settings based on this result alone.

## Implemented Boundary

- Explicit `interpret_incident_evidence` intent, fan-out only.
- Three required tasks: `interpret_dq_history`, `interpret_pipeline_run`, and
  `interpret_metadata_lineage`.
- Reuse Incident Triage and Metadata/Lineage specialist identities. Existing
  task model policies and default single-handoff behavior are unchanged.
- Exact task-specific read-only tool sets and `quickthinkllm` capability.
- Each worker loads authoritative alert context, collects bounded evidence, and
  may make one strict interpretation call through the existing supervised router.
- Per-worker capacity: one call, 8,192 tokens, USD 0.016, and 60 seconds.
  Parent acceptance capacity: three calls, 32,000 tokens, USD 0.05, concurrency 3.
- Deterministic plan hashing, unique worker identities, checkpoint namespaces,
  shared budget admission, typed results, and evidence-first aggregation reuse
  the existing fan-out executor.
- Model-proposed plans cannot remove or replace the three required categories.
- No report lifecycle update, data mutation, or remediation occurs in these workers.
- No-LLM results explicitly say interpretation was not run. They do not count as
  external-provider acceptance or assign a fabricated confidence score.

## Retained Airflow Evidence

| Scope | DAG / Run | Result |
| --- | --- | --- |
| Initial regression | DAG 91 / `manual__evidence_workers_regression_20260908T064200` | All five tasks succeeded; 888 pytest tests, 39 readiness checks |
| Operational evidence collection | DAG 98 / `manual__evidence_workers_offline_20260908T064300` | All five tasks succeeded; three workers, zero model calls, tokens, or cost |
| Paid attempt | DAG 98 / `manual__gemini_evidence_fanout_20260911T165230` | Failed; one attempt per worker, no successful model response |
| Transport diagnostic regression | DAG 91 / `manual__evidence_transport_regression_20260911T165600` | All five tasks succeeded; 894 pytest tests, 39 readiness checks |

The regression runs retained two existing dependency deprecation warnings
(Starlette/httpx and Discord/audioop). Web checks include HTTP/proxy behavior and
persisted report content in server HTML, not browser visual or hydration acceptance.

The offline parent UUID is `2df52f8b-30ed-5112-9b67-f528c4b211ea`.
The paid-attempt parent UUID is `5aa15895-1002-5460-a808-aab1e11fe7dd`.
The paid attempt ran from 16:52:38 to 16:52:55 Asia/Bangkok on 2026-09-11.
Its run task failed; verification, summary, and finish tasks were upstream-failed.
ClickHouse retained three worker failures and a blocked parent decision.

## Cost Interpretation

The failed attempt retained conservative reservations totaling 9,885 tokens and
USD 0.0135255 for three attempted calls. **These are reservation estimates, not
provider-confirmed token usage or invoice charges.** No successful
`llm_route_completed` event was retained. The provider dashboard remains the
source of truth for actual billing.

The temporary profile used `gemini-3.5-flash-lite`, structured output preferred,
1,600 maximum output tokens per call, and only Gemini plus the local heuristic
provider enabled. Each worker's one-call admission budget prevents an additional
provider attempt even when schema compatibility fallback would otherwise retry.
Acceptance rejects heuristic/provider/schema fallback rather than treating it
as Gemini success.

Profile SHA256:
`a2463f07cfd081a097f9455af47edaecbf5bb79444a278230241d3914ff6abd5`.

Configured text rates were checked against
[Google's Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing#gemini-3.5-flash-lite):
USD 0.30 input and USD 2.50 output per million tokens.

The runner was recreated from the normal configuration after the terminal failure.
`EXTERNAL_LLM_ENABLED=false` was verified; API remained disabled throughout.
No additional paid retry was made while DNS resolution was failing.

## Remaining Acceptance At The September 11 Attempt

1. Restore working DNS and HTTPS connectivity from `dq_runner` to the official
   configured Gemini endpoint. Do not bypass certificate verification or network policy.
2. Run one new manual DAG 98 three-worker acceptance with the isolated Gemini profile.
3. Require all three evidence categories, successful typed results, valid citations,
   exactly one Gemini completion per worker, and no fallback.
4. Reconcile each worker's token/cost totals with its own model audit event and
   the parent totals. Inspect DagRun state and every task log.
5. Review the actual generated interpretations for usefulness. This transport
   integration alone does not prove better diagnosis than the single-agent baseline.
6. Restore the default disabled LLM configuration and keep fan-out opt-in.

Safe transport diagnostics now distinguish fixed DNS, TLS, timeout, refused,
and reset codes when available in an exception chain. They never persist raw
connection strings, headers, credentials, or arbitrary exception messages.

## Successful Acceptance On September 24

Before fan-out, strict provider smoke DAG 92 run
`manual__gemini_smoke_20260924` succeeded on all five tasks. Gemini returned
127 input and 39 output tokens, estimated USD 0.0001356, with one request,
no fallback, and audit UUID `bbe66038-2f3e-46ba-93fa-4cb31eb83851`.
The response explained that zero raw orders violated the minimum and could
produce empty downstream dashboards. This was a real provider response, not
the heuristic baseline task in the same DAG.

The connectivity blocker was no longer present. No DNS override, TLS bypass,
or alternate provider was used. The first new run,
`manual__gemini_fanout_20260924T184000`, failed before inference because the worker
compared a human Alert Ref to the resolved system key. It made zero model calls.
Workers now accept the exact display identifier returned by authoritative alert
lookup, while still rejecting an unrelated identifier. Seven focused regression
cases cover all three worker types, lowercase references, and mismatched identity.

An offline DAG 98 run, `manual__alias_offline_fanout_20260924`, then completed all
five tasks successfully with three workers and zero external calls.

The paid run `manual__gemini_fanout_fixed_20260924T184500` completed successfully
through `98_dag_dq_control_plane_supervisor_smoke`. All five tasks succeeded on
attempt one. The run task, audit verification, and summary logs were inspected.
Parent UUID: `f1655e29-13d5-5181-b9fd-8f49b9b71378`.
Alert: `DQ-20260922-3218EA`, the synthetic missing-segment incident for September 22.

| Worker | Input tokens | Output tokens | Estimated USD | Model duration |
|---|---:|---:|---:|---:|
| Pipeline evidence | 3,796 | 414 | 0.0021738 | 3,644 ms |
| Metadata and lineage | 1,281 | 356 | 0.0012743 | 3,676 ms |
| DQ history | 1,070 | 590 | 0.0017960 | 4,682 ms |
| Total | 6,147 | 1,360 | 0.0052441 | Parallel; not additive wall time |

Every completion used `gemini-3.5-flash-lite` on `cheap_summary`, with validated
required structured output, no heuristic fallback, no provider fallback, and no
validation errors. Three worker completions and three matching LLM audit events
were read from ClickHouse; DAG audit verification reconciled 43 parent events,
three calls, 7,507 tokens, and the total above. The cap was three calls, 32,000
tokens and USD 0.05, with concurrency three and no retries.

The isolated route profile SHA256 was
`e73db85a8681dc7b44e0bd2dde9319b98d4c8812a71dead89a33cf302a1bd137`.
The runner was restored automatically and `EXTERNAL_LLM_ENABLED=false` verified.
These amounts are application estimates, not a provider invoice. This proves
bounded paid integration, not superiority over single-handoff investigation;
fan-out remains opt-in pending comparative quality evaluation.
