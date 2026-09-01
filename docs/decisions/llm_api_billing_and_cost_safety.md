####
## LLM API Billing and Cost Safety Decision for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# LLM API Billing and Cost Safety Decision

## Decision Status

Accepted as the working direction before enabling paid external LLM calls.

This decision records how the project should test external model providers without making normal development, Airflow validation, or daily pipeline runs unexpectedly expensive.

## Current Project Progress

The core data reliability platform is already established.

- Milestones 0 through 6 are complete: local infrastructure, synthetic landing data, ClickHouse loading, dbt transforms, deterministic DQ checks, agentic triage, and Airflow orchestration.
- Streamlit, Discord, FastAPI, MCP, metadata and lineage tools, schema drift handling, SQL review, checkpointing, and bounded supervisor pilots are substantially implemented.
- The current focused slice is bounded multi-agent execution: deterministic plan validation, isolated worker execution, fan-in aggregation, checkpoint reuse, and Airflow-first resilience evidence.
- Gemini prepaid provider acceptance is complete. Routine development remains deterministic, while external model use stays manual, budgeted, and disabled by default.
- The remaining work is mainly multi-agent quality comparison, operator UX polish, portfolio documentation, screenshots, and optional late-stage upgrades such as Next.js.

## OpenAI Billing Decision

ChatGPT subscriptions and OpenAI API usage are billed separately. A ChatGPT Plus or Pro subscription does not provide API credits.

Use OpenAI prepaid API billing for the controlled portfolio demo.

1. Create a dedicated OpenAI API project for this repository.
2. Add a payment method through the API billing settings.
3. Purchase the minimum initial credit amount of USD 5.
4. Disable auto-reload during setup because it is enabled by default.
5. Configure an enforced project hard spend limit of USD 5.
6. Configure organization-level protection when the organization is dedicated to this project.
7. Add early spend notifications, for example USD 1, USD 3, and USD 4.50.
8. Review the OpenAI Costs dashboard during external-provider testing.

Prepaid billing reduces exposure but is not an instantaneous hard cutoff. Provider-side processing delays can produce a small negative balance after credits are exhausted. Use prepaid balance, hard spend limits, application budgets, and an optional low-limit virtual card as layered controls rather than relying on one control.

Official references:

- https://help.openai.com/en/articles/9039756
- https://help.openai.com/en/articles/8264644-what-is-prepaid-billin
- https://help.openai.com/en/articles/9186755-managing-projects-in-the-api-platform

## Cost-Safe Testing Policy

External LLM calls must not be part of normal development acceptance by default.

| Tier | Validation Path | External LLM Policy | Default Cost Budget |
| --- | --- | --- | ---: |
| T0 | Direct unit and static tests | Prohibited; use mocks or heuristic mode | USD 0.00 |
| T1 | Airflow DAG 91 regression | Prohibited; use mocks or heuristic mode | USD 0.00 |
| T2 | Airflow DAG 92 provider smoke | Gemini, manual opt-in, one request | Maximum USD 0.01 |
| T3 | Full triage acceptance | Gemini, explicit manual execution | Maximum USD 0.05 |
| T4 | Multi-agent acceptance | Gemini, explicit manual execution and shared parent budget | Maximum USD 0.15 |

Default application budgets:

| Test Type | Model Calls | Aggregate Token Ceiling | Estimated Cost Ceiling |
| --- | ---: | ---: | ---: |
| Provider smoke | 1 | 4,000 | USD 0.01 |
| Full triage | 3 | 16,384 | USD 0.05 |
| Multi-agent default | 3 | 32,000 | USD 0.05 |
| Multi-agent hard limit | 10 | 64,000 | USD 0.15 |
| Testing campaign | Policy aggregate | Policy aggregate | USD 5 or remaining prepaid balance |

Required application controls before paid testing:

- Add a global `EXTERNAL_LLM_ENABLED=false` default kill switch.
- Keep paid-provider calls disabled merely because an API key exists.
- Restrict real-provider smoke tests to manual Airflow DAG 92 execution.
- Require an explicit provider and strict provider requirement for paid smoke runs.
- Permit at most one external model call for a provider smoke test.
- Use a maximum 4,000 aggregate token budget for provider smoke testing.
- Bound test output to approximately 500 to 1,000 tokens.
- Disable hidden SDK retries during supervised paid-provider testing.
- Reserve estimated tokens and cost before sending a provider request.
- Stop the run when the configured model-call, token, cost, or latency budget is exhausted.
- Persist provider, model, route, token estimate, estimated cost, duration, and fallback reason to audit evidence.
- Update provider model names and cost assumptions before each paid acceptance cycle.

Application-side cost estimates are guardrails, not billing records. The provider billing and usage dashboard remains authoritative.

## Provider Selection

Use different providers for different validation goals rather than calling every provider in every test.

### Default Development And Regression Testing

Use `no_llm_fallback` or heuristic mode.

Deterministic tests should validate routing, tool permissions, SQL guardrails, structured contracts, fallback behavior, audit records, and report assembly without paying for model calls.

### Low-Cost External Integration Smoke

Use Gemini 3.5 Flash-Lite with prepaid credit and synthetic project data only.

This route proves that the provider-agnostic OpenAI-compatible interface works without requiring OpenAI credit. The IDR 100,000 prepaid balance is the maximum available funding, not a spending target. Application budgets remain substantially lower than the prepaid balance, auto-reload remains outside the runtime, and no sensitive or real production data is included.

Official references:

- https://ai.google.dev/gemini-api/docs/billing
- https://ai.google.dev/gemini-api/docs/pricing

### Deliberate Portfolio Acceptance Demo

Use OpenAI GPT-5.6 Luna with prepaid credit and strict Airflow budgets.

This route provides a recognizable provider for portfolio evidence while remaining suitable for cost-sensitive workloads. It supports function calling and structured output.

Official reference:

- https://developers.openai.com/api/docs/models/gpt-5.6-luna

### xAI/Grok

Keep xAI/Grok disabled for routine testing. Its current pricing is materially higher than the selected Gemini and OpenAI routes, and the project does not currently gain enough additional validation value to justify that cost.

Official reference:

- https://docs.x.ai/developers/models

## Illustrative Request Cost

For an illustrative request containing 10,000 input tokens and 2,000 output tokens:

| Provider Route | Approximate Cost Per Request |
| --- | ---: |
| Gemini 3.5 Flash-Lite | USD 0.0080 |
| OpenAI GPT-5.6 Luna | USD 0.0044 |
| xAI Grok 4.6 | USD 0.0320 |

These calculations are estimates based on published token prices at the time of this decision. Actual costs vary with prompt size, output length, reasoning tokens, caching, tools, provider pricing changes, and retries.

## Final Working Direction

- Use heuristic mode for routine development, Airflow DAG 91 validation, and regression testing.
- Use Gemini 3.5 Flash-Lite only for explicit prepaid integration, full-triage, or bounded multi-agent acceptance through Airflow.
- Use OpenAI GPT-5.6 Luna for deliberate portfolio/demo acceptance after purchasing USD 5 prepaid credit.
- Keep Grok disabled unless a later evaluated use case justifies its higher cost.
- Keep `EXTERNAL_LLM_ENABLED=false` before and after every explicit provider acceptance run.
- Require every external model call to reserve model-call, token, estimated-cost, and latency budget before provider execution.
- Treat provider billing dashboards as authoritative because application-side estimates and provider-side billing updates can differ.

## Prior Gemini Acceptance Evidence

Gemini 3.5 Flash-Lite was acceptance-tested through the bounded manual Airflow
provider smoke path on 2026-08-31. No raw API key or provider response payload was
written to the retained operator evidence.

- DAG ID: `92_dag_dq_llm_provider_smoke`
- Final successful run ID: `manual__llm_smoke_gemini_35_final_20260831T070000`
- Provider and model: `gemini` / `gemini-3.5-flash-lite`
- External model calls: `1`
- Input and output tokens: `127` / `37`
- Estimated paid-tier reference cost: `USD 0.00013060`
- Provider duration: `1,668 ms`
- Fallback: none
- ClickHouse audit event: written successfully
- Final runtime state after acceptance: `EXTERNAL_LLM_ENABLED=false`

The earlier `gemini-2.5-flash-lite` attempt returned a model-not-found response,
and the moving `gemini-flash-latest` alias exceeded the 30-second request timeout.
The stable `gemini-3.5-flash-lite` identifier is therefore the selected project
default. Stable model identifiers are preferred over `latest` aliases so pricing,
behavior, and acceptance evidence remain reproducible.

## Gemini Prepaid Acceptance Evidence

Gemini 3.5 Flash-Lite was acceptance-tested again after the prepaid balance was
added. The run used exactly one provider request through the manual Airflow DAG,
then restored the external-provider kill switch. No raw API key or full provider
payload was written to retained operator evidence.

- DAG ID: `92_dag_dq_llm_provider_smoke`
- Run ID: `manual__llm_smoke_gemini_prepaid_20260901T125500`
- Agent run ID: `6358cc48-0f0d-447a-a5c6-f2e0463ae89b`
- Final DagRun state: `success`
- Provider and model: `gemini` / `gemini-3.5-flash-lite`
- External model calls: `1`
- Input and output tokens: `127` / `36`
- Aggregate tokens: `163`
- Estimated cost: `USD 0.00012810`
- Provider duration: `2,255 ms`
- Fallback: none
- ClickHouse audit event: verified in `dq.agent_audit_log`
- Final runtime state after acceptance: `EXTERNAL_LLM_ENABLED=false`

This evidence proves provider connectivity, route selection, usage capture, cost
estimation, audit persistence, and kill-switch restoration. It does not prove
that Gemini billing warnings are resolved permanently; provider billing status
and the provider dashboard remain external operational dependencies.
