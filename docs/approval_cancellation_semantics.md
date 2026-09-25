####
## Approval Cancellation Semantics for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# Approval Cancellation Semantics

## Purpose

Cancellation revokes permission to execute one bounded approval request. It is
not an Airflow DagRun cancellation mechanism.

The control plane intentionally separates two lifecycles:

1. Approval lifecycle: `pending`, `approved`, `rejected`, or `cancelled`.
2. Execution lifecycle: `not_started`, `dispatching`, `dispatched`, `succeeded`, or `failed`.

## Allowed Cancellation

A request may be cancelled only when all of these conditions are true:

- Approval status is `pending` or `approved`.
- Execution status is `not_started`.
- No dispatcher DagRun ID has been recorded.
- The caller supplies the configured control-plane approval token.
- The caller supplies a non-empty operator identity.

Cancellation is idempotent. Repeating cancellation for an already-cancelled
request returns its latest state without appending another lifecycle version or
audit event.

## Post-Dispatch Boundary

Cancellation is rejected after a dispatcher claims the request. The API does
not stop, clear, retry, or mutate an existing dispatcher or child DagRun.

An operator who needs to stop already-triggered work must use a separate,
explicit Airflow operational procedure. That procedure is outside the approval
revocation endpoint and must not be implied by the `cancelled` approval state.

## Audit Evidence

Successful cancellation appends a new latest-state version to
`dq.approval_requests` and writes one `approval_cancelled` event to
`dq.agent_audit_log`. The event records the previous approval state, current
execution state, operator, reason, and `airflow_runs_cancelled=0`.

## Local POC Concurrency Limitation

The current approval store uses append-versioned ClickHouse rows. ClickHouse is
the platform evidence store, but this implementation does not provide a
transactional compare-and-swap between API cancellation and Airflow dispatcher
claim.

For this local POC, the API and dispatcher enforce the same pre-dispatch state
rules and expose every transition through audit events. A production deployment
with concurrent operators should place approval coordination in a transactional
control store and keep ClickHouse as the analytical audit sink.
