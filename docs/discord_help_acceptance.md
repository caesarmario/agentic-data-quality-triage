# DataSentry Help Center Acceptance

## Scope

This slice adds deterministic onboarding to the existing green DataSentry bot. It does not change investigation, approval, Resolve, provider routing, warehouse data or public API endpoints. Live checks used the owner's authenticated desktop Discord client with the approved delegated test actions.

English is the default. `/help`, `/dq help`, and help-navigation replies are private. Direct mention `help` / `bantuan` replies to the source message; its buttons open private navigation. Shared welcome content remains English. Message Content Intent is still disabled.

## Implementation

- `apps/discord_bot/help.py`: local bilingual guide, registered-command inventory, separate help component namespace, scoped entry points, bounded incident lookup and sanitized error + guidance.
- `apps/common/locales/help.py`: curated Indonesian UI copy; no provider translation.
- `apps/discord_bot/welcome.py`: PostgreSQL-serialized welcome creation/update, durable delivery intent, uncertain acknowledgement reconciliation and confirmed-missing replacement.
- `infra/init/postgres/08_discord_service_messages.sql`: idempotent metadata-only registry, applied twice without deleting existing records.
- New help/welcome tests run through the existing DAG 91 allowlist. The gateway registers 24 invokable command paths including legacy aliases.

## Live desktop evidence, 2026-10-14

| Check | Observed result |
| --- | --- |
| `/help` | Private Quick Start; response `1559900928184950814` |
| `/dq help` | Private Quick Start; response `1559902104028446852` |
| Mention help | Source `1559902241991688213`, reply `1559902244051099691`; no AI conversation claim or investigation thread |
| Guide navigation | Three Commands pages, two Button Guide pages, Safety & AI and Quick Start edit the same private response |
| Show Incidents | Exactly five `active` results from the shared API; no model narrative or public incident-card republishing |
| Invalid date | `/dq daily_summary dt:tomorrow` reaches the handler; response `1559902404491747399` has error first and guidance second, private, with no exception class/raw argument |
| Indonesia preference | Saved through `/dq language`; welcome click creates private guide `1559902813369401426`, translated Quick Start and command inputs; shared welcome unchanged |
| Preference restoration | English restored through the client; PostgreSQL read-back `en` |
| Welcome restart | Same message ID before/after controlled restart; log reports `status=unchanged` |

The welcome remains message `1559709631083839519` in configured `dq-triage`. Its content hash is `3768793eceaea9fb03bb297badd3894a91968d1eb83a72973d599589794eafc3`. PostgreSQL records `creation_pending=false`, `edit_pending=false`, generation 0. Neither diagnostics nor a new startup announcement was sent to alerts/ops. A restart first checked that no Copilot or uncertain investigation claim was active.

The native desktop embeds, English/Indonesian copy, navigation controls, private visibility indicators and error-before-guidance hierarchy were inspected. Private screenshots are outside the public repository. No mobile claim is made.

## Failures retained and repaired

Earlier runs `manual__datasentry_help_discord_20261013T232955` and `manual__datasentry_help_all_20261013T232955` failed on outdated interaction test doubles. The repaired Discord run `manual__datasentry_help_fixed_discord_20261013T233424` passed 696 tests. Its all-suite counterpart failed one exact allowlist assertion because the new help/welcome files were absent from the expected list (2,056 passed, 12 skipped). These logs were retained, not cleared.

`manual__datasentry_help_regression_all_20261014T120848` then passed 2,057 tests, 12 skipped and 10 warnings, with all five tasks successful on attempt one and readiness 27/27. This predates the final navigation repair.

Live pagination exposed duplicate Discord component custom IDs: on the second page, Back and the section shortcut both targeted page zero with the same ID. Discord rejected the edit and the bot showed bounded error guidance. Navigation now has bounded `:back` / `:next` suffixes; existing IDs remain compatible. Tests verify unique IDs in every English/Indonesian page, identical REST/gateway component identities, and persistent reconstruction. All command/button pages were exercised again successfully.

Post-repair Discord run `manual__datasentry_help_navigation_discord_20261014T121410` passed 699 tests, 10 warnings and readiness 27/27. All five tasks succeeded on attempt one. Final post-change runs are recorded below after inspecting terminal states and retained logs.

## Audit and cost

During the live window beginning `2026-10-14T12:09:00Z`, ClickHouse retained successful help command/button/mention events and sanitized error events. At the inspected snapshot, no `llm_route_completed` or `copilot_answer` event existed in that window. The mention source has zero `discord_copilot_requests` claims. Guide error audit contains metadata correlation rather than raw command arguments.

Help/welcome and Show Incidents performed zero external LLM calls. API personal testing remains enabled; the Airflow runner remains disabled. No approval, closure, remediation or pipeline execution was initiated by a guide button. Model names or billing health are not implied by the onboarding footer.

## Final Airflow regression

DAG: `91_dag_dq_platform_validation`. Both post-change runs reached `success`; their retained pytest, readiness and summary logs were inspected.

| Suite | Run ID | Pytest | Readiness |
| --- | --- | --- | --- |
| discord | `manual__datasentry_help_final_discord_20261014T122406` | 699 passed, 10 warnings; 13.95 s | 27 passed, 0 failed |
| all | `manual__datasentry_help_final_all_20261014T122406` | 2,060 passed, 12 skipped, 10 warnings; 37.53 s | 27 passed, 0 failed |

For each run, `t00_start`, `t10_run_named_pytest_suite`, `t20_run_platform_readiness`, `t30_emit_validation_summary` and `t90_finish` all succeeded on attempt one. Readiness included ClickHouse, S3 and the required API dependencies. Existing warnings concern `audioop` and discord.py's legacy modal-label API; they were not suppressed. The 12 skipped all-suite cases are not counted as passed or live acceptance. The new scoped PostgreSQL welcome regression ran successfully with the installed migration.

After the final audit-label fix, the gateway was restarted with zero active/uncertain AI claims. It again logged the same welcome ID as unchanged. English was restored in the original private guide response as well as in durable preferences. No runtime logic changed after these final runs; subsequent edits only record this evidence. Direct pytest remains development feedback only.

## Limits

- Unknown slash commands are filtered by Discord's registered-command picker; DataSentry cannot promise a callback for every spelling error.
- Unmentioned text, DMs, bots, webhooks and foreign guilds are ignored. Help is scoped to configured channels and registered public triage threads.
- Backend outage, forged IDs, locked/missing threads, concurrent welcome creation and ambiguous delivery branches have focused regression coverage; they were not induced against the live server.
- Desktop acceptance used one owner account. Two-human isolation and mobile/narrow acceptance remain separate, previously deferred work.
- An uncertain create stays pending if bounded reconciliation cannot prove the result. Operators must inspect the existing intent; the bot does not blindly create another welcome.
- Localhost Workspace links work only on the machine running Streamlit. No remote hosting is implied.
- No commit or push is included in this slice.
