####
## Provider and Agent Runtime Boundaries for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# Provider and Agent Runtime Boundaries

## Decision Status

Accepted as the project direction for provider selection, local-model evaluation,
multi-agent debate, and external MCP clients.

This document records architecture boundaries, not provider availability or a
claim that every optional integration has been implemented or acceptance-tested.
Provider model IDs, endpoints, terms, quotas, and prices can change; verify them
against official provider documentation before enabling a route or running a
paid acceptance test.

## Provider Interface

Keep provider selection behind the provider-agnostic routing and client
interface. The application uses an OpenAI-compatible chat-completions interface
where supported, with provider endpoint, credentials, and model configured
outside agent logic. Workflow orchestration remains in LangGraph; a model
provider is not a replacement for the workflow engine.

The supported interface is an interoperability boundary, not a promise that
every provider implements every OpenAI API feature identically. Structured
output, request fields, retry behavior, token accounting, and error semantics
must be smoke-tested for each configured provider/model combination.

Project evidence:

- `agent/llm/config.py` defines `ProviderType` as `heuristic` or
  `openai_compatible` and resolves route configuration independently from
  provider credentials.
- `agent/llm/client.py` executes configured routes through the
  OpenAI-compatible client and records the selected provider, model, token
  counts, and estimated cost.
- `docs/decisions/llm_api_billing_and_cost_safety.md` documents the project
  route policy, cost controls, and prior Airflow provider acceptance evidence.
- `docs/agent_supervisor_lite_architecture.md`, under **LLM Boundary**, states
  that the LLM is an optional reasoning or narrative helper, not the
  orchestrator.

## Provider Discovery and Verification

Third-party provider directories and comparison lists, including
`tokengratis.id`, are discovery sources only. They must never be copied into
runtime configuration as an endpoint, model registry, pricing authority, or
trust signal. A listing does not establish provider ownership, security,
availability, compatibility, or suitability for project data.

Before configuring or enabling a provider route, verify all of the following
using official provider documentation and the actual account/project settings:

- The official API endpoint and authentication method.
- The exact supported model ID, including whether it is stable or an alias.
- Current input/output pricing, free-tier limits, and applicable billing terms.
- Supported request features used by this project, including structured output
  and token usage reporting.
- Quotas, rate limits, data handling terms, and any account or project
  prerequisites relevant to the test.

Record the verification date and the official sources in the relevant provider
acceptance record when a route is tested. Recheck these details before a later
paid test; checked-in values are configuration snapshots, not permanent
availability guarantees.

## Local and Self-Hosted Models

Evaluating a local or self-hosted model provider is deferred. The current
heuristic fallback is deterministic local code, not a language model and not
evidence that a local model runtime is supported.

Do not describe local-model support as implemented until a real local model
runtime has been selected and acceptance-tested end to end. That evaluation
must cover runtime startup and health, model identity, request/response
compatibility, structured-output behavior where required, timeout and failure
handling, resource use, and the project routing/audit path. Keep this optional
evaluation separate from the existing external-provider route acceptance.

## Multi-Agent Debate

Multi-agent debate is explicitly deferred. The current same-incident LIFE
comparison retained `keep_single`: fan-out collected additional evidence
references, but did not measurably improve confidence or evaluated report
quality. This is evidence against enabling debate by default; it is not proof
that debate could never help a future evaluated use case.

Bounded fan-out and debate are different execution patterns. The implemented
fan-out schedules scoped specialist workers, isolates their context and
permissions, and aggregates typed results. It does not create independent
agents that exchange arguments, critique one another, or vote on a conclusion.
Do not present fan-out acceptance as debate support.

Reconsider debate only after a concrete scenario demonstrates a gap that
deterministic evidence gathering and bounded fan-out cannot address. Any
proposal must use the same ground truth as the single/fan-out baseline and
measure diagnostic quality, unsupported claims, latency, token/cost usage, and
failure containment. Debate must remain opt-in unless it shows a material,
repeatable benefit and receives an explicit policy decision.

Project evidence:

- `docs/agent_reliability_evaluation.md`, under **Accepted Comparison
  Baseline**, records a same-alert comparison with three more fan-out evidence
  references but a `0.000` report-quality delta and the `keep_single` decision.
- The **September 27 Missing-Segment Safety Regression** section records the
  newer comparison: fan-out added three references, confidence and report
  quality deltas were both `0.000`, and the decision remained `keep_single`.
- `docs/agent_supervisor_lite_architecture.md`, under **Same-Incident Quality
  Gate Evidence**, records the same result and states that a later policy
  change requires a measured improvement and human approval.

## OpenClaw and MCP

OpenClaw may be considered only as an optional external MCP client. It is not
the platform's workflow orchestrator, supervisor, or agent runtime. Existing
generic MCP support does not prove OpenClaw compatibility or acceptance.

The current project MCP server exposes the project's guarded tools through
FastMCP and the checked-in command currently permits only the `stdio`
transport. No OpenClaw-specific client configuration, connection, or
end-to-end acceptance is recorded by that generic MCP implementation. Any
future OpenClaw integration must be tested as a separate client integration
against the actual transport and tool contracts, without broadening tool
permissions or bypassing supervisor policy and approval boundaries.

Project evidence:

- `agent/mcp/server.py` registers the current MCP tool surface and constrains
  `--transport` to `stdio`.
- `docs/agent_supervisor_lite_architecture.md`, under **LLM Boundary**,
  describes MCP as an interface around the existing guarded tools rather than
  a replacement orchestration layer.

## Review Triggers

Revisit this decision when one of these conditions is met:

- A provider is proposed for a new runtime or paid acceptance route.
- A real local/self-hosted model runtime is available for end-to-end testing.
- A LIFE evaluation identifies a specific case where debate may improve
  investigation quality beyond bounded fan-out.
- An OpenClaw client integration is proposed with a concrete transport and
  acceptance plan.

Any change to the production default must be separately evaluated, documented,
and approved. A provider listing, successful generic MCP tool call, or
additional fan-out worker count alone is insufficient evidence.
