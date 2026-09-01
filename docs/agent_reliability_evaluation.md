####
## Agent Reliability Evaluation Loop
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# LIFE-Inspired Reliability Boundary

The project uses LIFE as an evaluation and improvement discipline, not as an
autonomous self-modifying agent. A failed evaluation can create a proposal for
human review, but it cannot edit prompts, routing policy, DQ rules, SQL
guardrails, DAGs, tools, or remediation behavior.

## Project Stage Map

### Lay Foundation

Guarded tools, deterministic DQ checks, typed state, incident ground truth,
approval contracts, and append-only audit evidence establish the behavior that
an evaluation is allowed to trust.

### Integrate Collaboration

Supervisor-lite LangGraph nodes and typed specialist handoffs combine evidence
without sharing hidden prompts, unrestricted conversation history, credentials,
or raw environment state.

### Find Faults

DAG `94_dag_dq_agent_life_evaluation` compares a retained triage report with an
allowlisted incident scenario. Checks cover report structure, root cause,
evidence integrity, confidence, SQL safety, action safety, provider fallback,
and stakeholder readability.

### Evolve Safely

The evaluator produces a bounded change type and written proposal. Every
non-passing proposal requires human review before it can become backlog or PR
work. Evaluation artifacts and the corresponding audit event retain the source
report hash so reviewers can prove which evidence was scored.

## Optional Critic

The critic is disabled by default. When explicitly enabled for a `review` or
`fail` result, it challenges the evaluator before the proposal is accepted. It
asks whether stale evidence, scenario configuration, alias mapping, or contract
drift could explain the result and lists the failed checks a reviewer must
inspect.

The critic is deterministic and does not call an external LLM. This prevents a
second model from turning disagreement into unbounded cost or another source of
hallucination. A stronger model-backed critic can be evaluated later, but it
must keep the same typed output and human-approval boundary.

## Operational Flow

```text
retained triage report
  -> deterministic evaluator
  -> optional bounded critic
  -> human-review improvement proposal
  -> JSON and Markdown artifacts in SeaweedFS
  -> append-only ClickHouse audit event
```

## Acceptance Evidence

Final acceptance is Airflow-first. Trigger DAG 94 with `enable_critic=true`,
inspect every task state, verify both artifacts, and confirm the matching
`life_evaluation_completed` event in `dq.agent_audit_log`. Direct pytest remains
inner-loop feedback only.

## Single Versus Fan-Out Evaluation Gate

Bounded fan-out is not considered better merely because it creates more workers.
Before it can replace single-handoff execution, DAG 94 must evaluate both modes
against the same incident scenario, source evidence, and ground-truth contract.

The comparison must retain:

- evidence categories requested and successfully collected
- required and optional worker failures
- root-cause match and unsupported-claim checks
- confidence and missing-evidence disclosure
- model calls, input/output tokens, estimated provider cost, and fallback reason
- elapsed latency, worker count, and peak concurrency
- final action safety and human-approval requirement

Fan-out may become the default only when it provides measurable evidence
coverage, investigation quality, or fault-isolation benefit without unacceptable
cost or latency. A tie keeps single-handoff as the default because it is simpler
to operate. Evaluation output may propose a routing change, but cannot enable
fan-out or modify policy automatically.
