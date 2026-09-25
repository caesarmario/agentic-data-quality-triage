<!--
Premium Operator UI Testing Guide
Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-->

# Web Operator Testing

## Scope

The optional Next.js UI uses the existing FastAPI control plane. Streamlit remains
the internal/debug console. Opening pages reads warehouse and artifact evidence;
it does not invoke an LLM or execute remediation. A triage button is an explicit
new workflow request, not part of the read-only acceptance checks below.

## Start Locally

From the repository root, with the existing Docker stack running:

```powershell
docker compose --env-file infra/.env -f infra/docker-compose.yml --profile web up -d --build --wait web
docker compose --env-file infra/.env -f infra/docker-compose.yml --profile web logs --tail 50 web
```

The default browser address is `http://localhost:3000`. The service binds only
to the host loopback interface. `WEB_PORT` changes the published port and the
trusted browser origin together. Keep `EXTERNAL_LLM_ENABLED=false` for routine
testing. The web container does not receive the shared `.env`, warehouse keys,
or provider API keys.

## Airflow Acceptance

After the web image is rebuilt and healthy, run:

```powershell
docker exec dq_airflow_scheduler python /opt/airflow/project/scripts/trigger_airflow_validation.py --suite ui --require-api --require-web
```

Retain the printed run ID. Inspect the run and all task states in DAG
`91_dag_dq_platform_validation`, then read `t10_run_named_pytest_suite` and
`t20_run_platform_readiness` logs. Success requires the web service checks,
not just an independently successful API health check.

The web probe uses invalid, non-executing approval requests to verify origin,
JSON, payload-size, and route restrictions. It must never approve a real request,
trigger triage, or run Airflow backfill just to test the proxy.

To require a real stored report as well, add `--require-web-report`. This stricter
gate discovers a triaged alert and its paired report JSON, checks alert identity,
and compares seven report/heading text fields with the selected server HTML.
It fails if no suitable stored report exists. Script/template/hidden markup alone
cannot satisfy the text checks. It does not invoke Gemini or produce a new report.

```powershell
docker exec dq_airflow_scheduler python /opt/airflow/project/scripts/trigger_airflow_validation.py --suite all --require-api --require-web-report
```

## Manual Operator Checks

1. Open Reliability Overview and distinguish the selected data date from today's date.
2. Open Incident Center and switch between open, triaged, and resolved alerts.
3. Select one alert. Its human Alert Ref must match the incident and report shown.
4. Open Triage Workbench. Read the existing report, hypotheses, evidence, and cost
   information without pressing Run triage. Missing telemetry must say it is missing.
5. Confirm that an invalid explicit alert reference shows an unavailable state,
   rather than silently selecting an unrelated alert.
6. Inspect Approval Queue without approving anything. Approval is separate from
   execution; it is not an automatic repair button. Verify that a pending
   pre-dispatch request offers Approve, Reject, and Cancel; an approved
   pre-dispatch request offers only Cancel; and claimed or terminal requests
   offer no mutation controls. A successful decision should refresh the queue.
7. Use Lineage/Blast Radius with `dq.raw_orders` and inspect bounded downstream results.
8. Check keyboard focus, narrow/mobile layout, long text, and light/dark contrast.
   At 390px, the theme toggle must remain visible below the navigation and
   switch between light and dark without changing the selected page.

HTTP acceptance proves service behavior, not visual quality. Screenshots,
keyboard interaction, responsive layout, and browser rendering need a separate
visual review; do not mark them complete from HTTP status codes.

## Retained Acceptance

On 2026-09-08, DAG `91_dag_dq_platform_validation` completed run
`manual__web_acceptance_20260908T061418` successfully, from 06:14:34 to 06:15:02
in Asia/Bangkok. All five tasks succeeded. Retained task logs show 840 pytest
tests passed (two warnings) and 38 readiness checks passed, including all 12
web checks. This run used `validation_suite=all`, `require_api=true`, and
`require_web=true`.

The deployed image was
`sha256:5bc661c958c1b61862948dad06f2d3f2ec63c2a05a31ea8f2aa0bf69c0cf355e`.
Its build ran 39 frontend contract/policy tests and passed the production build.
The Node test runner also reported a non-failing module-type inference warning.
Runner and API external LLM permissions remained disabled.

The 12 web checks cover five HTTP page responses, a real proxied alerts response,
and six rejected proxy request cases. That first run did not include real report
content acceptance and did not prove browser interaction or visual layout.

The subsequent run `manual__web_report_20260908T062131` completed successfully
from 06:21:43 to 06:22:04 Asia/Bangkok, with all five tasks successful, 852 pytest
tests passed (the same two deprecation warnings), and 39 readiness checks passed.
It verified stored report `RPT-27BDC120`, including matching alert identity and
seven expected document text fields. The test used only GET requests for report
inspection and made no external model call. The deployed web image was unchanged.

This proves server-emitted report content, not computed visibility, styling,
responsive layout, keyboard behavior, or browser hydration. Those checks and
actual approval/triage interactions remain pending.

## September 24 Browser And Regression Follow-Up

The optional web image was rebuilt as
`sha256:be9bd26bb8f71b350727ccfc578e125dc88bbce8ca784ded82e16062a97a7bd7`.
The build passed 42 frontend policy/contract tests, TypeScript, and the Next.js
production build. The existing Node module-type inference warning remains.

Headless Chromium visited all five pages at 1440x1000 and 390x1000, in both
light and dark themes: 20 combinations. Every page returned HTTP 200, exposed
five navigation links and a visible theme toggle, and had no page-level
horizontal overflow. The toggle was activated with keyboard Enter and checked
against both DOM state and final computed background color. No pageerror or
console error was recorded. Tables/navigation may intentionally scroll within
their own containers on mobile.

Real overview and stored-report captures were visually inspected and retained
under `docs/images`. The dark report shows `RPT-87CB9E0E` for
`DQ-20260922-3218EA`, generated through the no-LLM DAG 98 walkthrough.
Duplicate logical alerts no longer appear after reading current ReplacingMergeTree
versions with FINAL. Approval controls are hidden after dispatch or execution;
unit tests cover that policy, but this pass did not click a real approval action.

Airflow DAG 91 run `manual__acceptance_hardening_20260924T184200` retained
937 passed pytest tests, two dependency deprecation warnings, and 39 passing
readiness checks. Report readiness matched seven stored report fields with no
external model call. This adds authoritative code/service acceptance; browser
checks are complementary evidence, not tests executed inside that DagRun.

Remaining: real browser approval/triage action acceptance on dedicated synthetic
requests, complete keyboard navigation and accessibility review, and fresh-clone
installation. No claim of enterprise authentication or production readiness is made.

## Final Theme Follow-Up

Visual review found fixed white text on mint controls in dark mode. The final
image `sha256:80c796afded800d329239ea0ea0f9a0e1a85e5c6ddc0977b09d757b4b4baf37f`
uses a theme-specific foreground for solid controls and explicit brand text color.
Twelve source checks enforce normal-text contrast of at least 4.5:1 for accent,
success, and danger controls in both themes. The repeated 20-case browser pass
measured 6.45:1 light and 10.57:1 dark for active navigation, brand marks, and
primary buttons where present. No hydration errors or page overflow were found.
Screenshots under `docs/images` were refreshed from this final pass.

DAG `91_dag_dq_platform_validation`, run
`manual__theme_acceptance_20260924T185000`, succeeded on all five tasks, attempt
one. Logs retained 950 passed pytest tests, the same two dependency warnings,
and 39 readiness checks, including stored report identity/content. The final
DagRun and task states were rechecked on September 25 after Docker restart.
External LLM remained disabled. Airflow's import-error list was empty after
switching the helper import to its absolute package path.

## September 25 Operator Interaction Evidence

A dedicated synthetic request, `APR-20260923-50763ABF`, was approved and then
cancelled through the real browser proxy. Blank operator identity was rejected
before any POST. Keyboard Enter activated both decisions, and successful queue
refreshes removed unavailable controls. The final durable record is `cancelled`,
with execution `not_started` and no execution DagRun ID. No dispatch was invoked.
This was not a dry-run request: the operational API stores `dry_run=false`; safety
came from the explicit no-dispatch test boundary and subsequent cancellation.

The initial fixture setup exposed a real audit serialization failure: Python
`date` values in backfill ranges were not supported by the audit JSON writer.
The writer now serializes business dates as ISO dates, with regression coverage
through the actual writer. The partially created fixture `APR-20260922-BCD107AD`
was cancelled with an explanatory comment, not deleted or treated as successful
acceptance. Approval storage and audit storage remain non-transactional.

Browser triage for `DQ-20260922-3218EA` produced `RPT-AA36D57C`, run
`3a1b1526-ae40-40c9-a864-99a412e908fb`. Twelve audit events include three heuristic
route events, report storage, and triage completion. The completion record has
`external_model_used=false` and estimated cost zero. No remediation was approved
or executed by triage. Both API and runner retained external LLM disabled.

Tab traversal reached all five primary navigation links on each page at both
1440px and 390px. Browser checks remain complementary to the retained Airflow
validation, not a claim that Playwright ran inside Airflow. This is a bounded
local demo check, not a complete accessibility certification.

The follow-up web image
`sha256:c9ca0eaffc5fe90912d1b274c39b47573e530e8a4c4403d93aad490f1bccc8bc`
refreshes the server-rendered report after successful triage and announces the
result as a polite status region. The mobile browser pass produced
`RPT-286D11F6`, run `65b9c887-15af-4da7-836d-5860415e6c3f`, with exactly one real
triage POST. The persisted report updated without page reload. A separately
intercepted synthetic HTTP 503 displayed an error, restored the trigger button,
removed the previous success panel, and preserved the stored report. It did not
contact the API for a second investigation. The image passed 42 frontend tests,
TypeScript, and the production build.

## Local Security Boundary

The browser proxy exposes exact reviewed routes, not arbitrary URL prefixes.
Mutation requests require the configured Host, matching Origin, JSON content,
and a bounded object body. Approval credentials are injected on the server only
for the reviewed approval paths. Redirects are not followed upstream.

These controls do not provide multi-user authentication or role-based access.
This is a localhost operator console. Add authenticated users, authorization,
TLS, and deployment-specific origin policy before exposing it publicly.

## Stop Only The Optional UI

```powershell
docker compose --env-file infra/.env -f infra/docker-compose.yml --profile web stop web
```

This leaves Streamlit, Airflow, the API, and warehouse services running.
