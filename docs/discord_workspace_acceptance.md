# Discord Workspace Acceptance

This ledger covers the Discord incident workspace and language slice. It separates automated Airflow evidence, read-only artifact checks, and human-operated Discord/Streamlit checks. A passing DAG does not establish that a human completed a Discord interaction.

## Current interaction contract

- One canonical public thread per incident in `dq-triage`; `dq-alerts` holds the compact durable incident card.
- Active card row 1: **Investigate** (primary), **Evidence**, **Ask AI**. Row 2: optional **Workspace**, **Resolve** (secondary).
- Resolved card: **Evidence**, optional **Report** when available, and **Workspace**.
- **Investigate** reads the latest persisted report. **Run New Investigation** explicitly reruns it. Do not retry a call whose outcome is uncertain; duplicate model calls are not a recovery strategy.
- `/dq language language:English|Indonesia` stores a per-guild, per-user preference. Shared cards and thread starters are English; personal answers follow the preference. Streamlit language is per session. Raw reports and operator reasons remain untranslated.
- Guarded reports are at most 200 KB. Show the authoritative JSON URI and Markdown sibling only, with copyable URI and bucket/key code blocks. An `s3://` locator is not a web URL. No signed URLs or public buckets.
- Resolve requires matching fresh `PASS` evidence (maximum age ten minutes), an operator reason and explicit confirmation. It does not call an LLM or repair data. Copilot treats expired PASS as historical and recommends **Resolve -> Check Again**, not `/dq triage`, for closure proof.
- The expiry gate applies before confirming an active incident. It does not undo a saved RESOLVED decision. For a closed incident, Copilot recommends viewing recorded evidence and continued monitoring, not closing it again. Expired or completed final confirmation panels retire their controls; an ambiguous API outcome must be reconciled through current state.
- Newly created incident threads include the human-readable rule description, bounded to 80 characters. Existing thread names and message history are not rewritten.
- Cancellation while the PostgreSQL investigation claim is settling records `cancelled_before_start` only when triage never started. The same request ID remains non-replayable; a new interaction can investigate normally. Cancellation after triage starts remains `uncertain`, with no automatic provider replay.

## Airflow 91 deterministic validation

The following named runs were reported as current evidence for the workspace changes. Each had all five DAG tasks succeed and 27/27 readiness checks pass:

| DAG run | Suite result | Readiness |
| --- | --- | --- |
| `manual__workspace_resume_discord_20261012T131134` | 572 passed, 10 warnings | 27/27 |
| `manual__workspace_resume_api_20261012T131134` | 256 passed | 27/27 |
| `manual__workspace_resume_ui_20261012T131134` | 272 passed | 27/27 |
| `manual__workspace_theme_final_all_20261012T140118` | 1,906 passed, 12 skipped, 10 warnings | 27/27 |
| `manual__workspace_postclosure_final_discord_20261012T143737` | 600 passed, 10 warnings | 27/27 |
| `manual__workspace_postclosure_final_api_20261012T143737` | 266 passed | 27/27 |
| `manual__workspace_postclosure_final_ui_20261012T143737` | 275 passed | 27/27 |
| `manual__workspace_postclosure_final_all_20261012T143737` | 1,922 passed, 12 skipped, 10 warnings | 27/27 |
| `manual__workspace_native_scheme_ui_20261012T224015` | 276 passed | 27/27 |
| `manual__workspace_native_scheme_all_20261012T224015` | 1,923 passed, 12 skipped, 10 warnings | 27/27 |
| `manual__dq_snapshot_isolation_api_20261012T235420` | 312 passed | 27/27 |
| `manual__dq_snapshot_isolation_ui_20261012T235420` | 276 passed | 27/27 |
| `manual__dq_snapshot_isolation_all_20261012T235420` | 1,975 passed, 12 skipped, 10 warnings | 27/27 |
| `manual__desktop_docs_final_all_20261013T000807` | 1,975 passed, 12 skipped, 10 warnings | 27/27 |
| `manual__workspace_cancel_claim_discord_20261013T120552` | 626 passed, 10 warnings | 27/27 |
| `manual__workspace_cancel_claim_all_20261013T120552` | 1,987 passed, 12 skipped, 10 warnings | 27/27 |

The latest all-suite run includes the confirmation-session, recorded-closure Copilot context, readable thread-name, native live-scheme, outbox-recovery, and DQ-date-isolation fixes. Its five tasks (`t00_start`, `t10_run_named_pytest_suite`, `t20_run_platform_readiness`, `t30_emit_validation_summary`, `t90_finish`) and final DagRun were `success`, each on its first attempt; retained pytest and readiness logs were inspected. Warnings and skips are retained, not silently described as clean. All named suites above also had every task succeed.

The final desktop/documentation run explicitly required API readiness. It also retained zero DAG import errors. No external provider call was made by the regression suites. This row was recorded only after its terminal state and retained logs were read.

The subsequent cancellation hardening runs also required API readiness. Each finished with all five tasks `success` on attempt one; pytest and readiness logs were read. The Discord suite includes four real PostgreSQL tests using newly created `workspace_claim_fixture_` rows, alongside delayed-claim cancellation and repeated-cancellation tests. Those fixtures were cleaned up; no previous claim or incident was modified. This is database/regression acceptance, not two-human live acceptance.

## Airflow 92 provider acceptance

All five tasks succeeded in each of the five named DAG 92 runs. Each used Gemini 3.5 Flash Lite, completed without heuristic fallback, and was a deliberate provider call rather than a routine test. Token and cost figures are run-time estimates, not a current price guarantee.

| DAG run | Language/context | Agent run ID | Tokens | Estimated cost |
| --- | --- | --- | ---: | ---: |
| `manual__workspace_fresh_provider_en_20261012T131906` | English | `4c91db73-346a-4e92-b9a6-1665b8aa8dc4` | 127 in + 39 out = 166 | USD 0.0001356 |
| `manual__workspace_fresh_operator_language_en_20261012T131906` | English | `48f6dd09-28f1-414e-bc82-30c641c2bbd6` | 2,226 total | USD 0.0010836 |
| `manual__workspace_fresh_operator_language_id_20261012T131906` | Indonesian | `d75e2a95-12e7-4999-98dc-2d05a53b9963` | 2,368 total | USD 0.00119 |
| `manual__workspace_fresh_action_en_20261012T132445` | English expired-evidence next-action case | `9a6f2672-54bc-40f7-b451-7d2344a8c929` | 2,483 total | USD 0.0010353 |
| `manual__workspace_resolved_context_en_20261012T142330` | English recorded-closure case | `d0a99afa-6ae9-4ae5-81f1-99ff88de1b16` | 2,579 total | USD 0.0010333 |

The four calls total USD 0.0034445 estimated cost. The first language answer correctly identified expiry but initially gave a poor next command; the prompt was revised. The fresh-action run then explicitly recommended **Resolve -> Check Again** and described the current state as unverified. A safe operator-language DAG 92 configuration sample is:

```json
{"acceptance_case":"operator_language","language":"en","alert_ref":"DQ-20261011-3E3C14","run_external_provider":true,"route_name":"cheap_summary"}
```

Use `"language":"id"` for the Indonesian case. `scripts/smoke_operator_language.py` is the focused smoke entry point. These provider results do not replace deterministic DAG 91 checks or live UI acceptance.

The fifth DAG 92 call followed a live finding: the model initially treated expiry as a reason to re-close an already resolved incident. The context now supplies the authoritative recorded closure separately from check freshness. The new strict run correctly stated that the incident is already closed and recommended viewing closure evidence and normal monitoring. All five tasks succeeded, retained logs and ClickHouse provider audit were inspected, and no fallback occurred. The five DAG-backed calls total USD 0.0044778 estimated cost.

## Read-only report evidence

- Report `RPT-7CA81EA1` was read through the API without triggering an investigation or provider call.
- The Markdown artifact was 7,849 bytes; the JSON artifact was 145,356 bytes, both within the 200 KB guard.
- The API response exposed the authoritative JSON URI and Markdown sibling. This was an API read proof, not proof that the Discord Report button was live-verified.

## Discord live evidence and limits

- Message `1558869741165215817` is a DataSentry-authored card (author `1476800539747553463`) recorded with `discord_bot_api` transport.
- Human interaction with Evidence created canonical thread `1559193662208868435`; the Evidence response was `1559193668169113641`.
- Initial readback contained only Evidence despite the human's confirmation. After the owner opened an authenticated Discord browser tab, a live browser click on Investigate delivered investigation message `1559198821874139299` into the same thread. The interaction audit `b2030e10-b978-4d02-991b-8a8c8cc7a2cc` recorded success and reused the stored report, with no new provider call.
- A live browser click on View Report delivered message `1559199279120261268`, with both `report-RPT-7CA81EA1.md` and `report-RPT-7CA81EA1.json` in that thread, together with separate copyable object locator, bucket and key fields. This is live browser interaction evidence, not a claim of human-operated approval or closure.
- Discord desktop card layout was inspected in the authenticated client. New active-card buttons use two rows; the existing schema closure card was refreshed in English on message `1558874489020874804`, with its original operator reason preserved and no stale synchronization warning. Discord mobile remains unverified.
- Streamlit English and Indonesian wide layouts were inspected in dark and native Light mode. Native canvas ownership corrected a mixed light-sidebar/dark-main rendering defect. Locale changes preserved canonical selections; the newly closed incident disappeared from Active after refresh. The viewport override did not change the actual browser content width, so narrow/mobile acceptance is not claimed. The original System theme and English locale were restored.

### Successive modal and tagged follow-up

Two authenticated modal requests and one delegated tagged question were answered in canonical thread `1559193662208868435`, without creating another thread or changing the incident reference:

| Source | Answer message | Agent run | Tokens | Estimated cost |
| --- | --- | --- | ---: | ---: |
| Delegated initial modal | `1559208766082121799` | `781cd660-75d2-4651-ae7d-718cd090bbe7` | 2,269 | USD 0.0010217 |
| Subsequent authenticated modal | `1559210222524829707` | `4e9e3f9a-0690-421e-a17f-5ed57c1ee27d` | 2,736 | USD 0.0010540 |
| Tagged message `1559211911105478728` | `1559211931573821560` | `18a68a4e-54f0-4b8d-af97-2750d526a30a` | 2,857 | USD 0.0010529 |

The initial answer exposed the recorded-closure/expiry problem and remains in history, rather than being hidden. The later modal and tagged answer used the corrected context. The tagged question omitted Alert Ref, received a check-mark reaction and a requester-only ping, and correctly stated that expiry does not undo the saved closure. Its API audit records two prior conversation pairs and 1,367 characters, within the 4,000-character bound. Durable request records are `answered`, scoped to the same incident/thread/actor. Gemini 3.5 Flash Lite executed once per request without heuristic fallback.

The three live calls total USD 0.0031286; combined with the five named DAG 92 calls, the eight correlated calls in this ledger total USD 0.0076064 estimated cost. This is not total account usage: personal testing remains enabled, and the provider dashboard is the billing authority. These checks establish one authenticated actor's live context continuity, not two-human isolation.

### Delegated DQ closure, 2026-10-12

The owner's earlier confirmation session had expired without a closure decision. After explicit owner delegation, the coding agent used the authenticated Discord browser to open Resolve, request a fresh check, submit a factual reason, and click the final confirmation. This is delegated live browser acceptance, not an assertion that the owner manually completed that final click.

- DAG: `90_03_dag_dq_incident_revalidation`; run: `manual__incident_revalidation_cf7c92ba-f3c9-4b1e-ac1c-b8f0c04c1d26`.
- All six tasks (`t00_start`, `t10_load_request`, `t20_run_scoped_checks`, `t30_persist_validation_result`, `t40_notify_validation_result`, `t90_finish`) and the DagRun were `success`. Retained logs confirmed the exact rule and positive-row prerequisite both passed.
- Request `cf7c92ba-f3c9-4b1e-ac1c-b8f0c04c1d26` found all 25 expected country/channel segments on `dq.fct_orders_daily`, business date `2026-10-11`.
- At `2026-10-12T14:09:35.993648Z`, incident `DQ-20261011-3E3C14` became `resolved`, revision 3. PostgreSQL contains one closure decision.
- Reason: `Delegated demo closure: the fresh read-only Airflow check confirmed all 25 required segments. No data repair was executed by this closure.`
- The incident card remained message `1558869741165215817`, author `1476800539747553463`, with no webhook identity. The resolved card has Evidence, View Report, and Workspace only.
- Interaction audit `aad0da0f-2d68-4ce7-9b03-878920ce4768` and lifecycle audit `6f5d1f80-1c6b-4b04-9015-81768c4e7b9e` recorded success. The outbox projection, audit, and delivery flags are all complete; the current preview reports `synchronization_pending=false`.
- The closure did not call an LLM, repair data, dispatch remediation, or declare the entire warehouse healthy.

### Native theme switching, 2026-10-13

The browser exposed `color-scheme: dark` on native `stApp` while server-generated custom colors still held the preceding Light values. This made inactive tabs and the unselected language label nearly invisible until a rerun. Scoped CSS `light-dark()` tokens now follow the native scheme without overriding canvas colors or requiring a rerun on every theme change. Older browsers without this function retain the server-resolved fallback palette.

Actual desktop views were inspected at 1,358 CSS pixels in English/Indonesian and Light/Dark. Mode changes immediately updated metric surfaces and control text: Light used white metrics with dark text; Dark used `rgb(20,29,25)` metrics with `rgb(235,244,239)` text. The original System theme and English preference were restored. Private screenshots were retained outside the public repository. Narrow/mobile rendering is still not established.

The owner confirmed that a second Discord user is not currently available. Two-user context and live cross-interface race acceptance remain pending, not silently replaced with one actor or a mock.

Still pending: two-human context and race behavior. On 2026-10-13, the owner explicitly prioritized desktop and deferred Discord mobile and narrow responsive views. Those visual checks remain unverified, not automatically passed because desktop works. No complete cross-interface live acceptance is claimed. These checks must not be replaced by fabricated screenshots, API-only checks, or repeated external calls.

Live fixture-scoped application fault/replay is now proved by DAG `90_04`, run `manual__outbox_fixture_live_20261012T231705`, incident `DQ-20261013-1C69EA`: one decision, the same event, and message `1559344212544069734` continued from OPEN to RESOLVED after replay. Initial adapter failures and exact task resumption are retained in [the recovery ledger](resolution_outbox_acceptance.md). Older decisions/flags were not reset. This is not a real Discord outage or simultaneous-human test.

### Daily snapshot isolation and final desktop check, 2026-10-13

The schema acceptance fixture initially advanced the daily summary date to `2026-10-13`, even though no business DQ checks existed on that date. Latest-date resolution now uses DQ check dates and `dq_failure` alert dates only; schema detection dates do not choose a business DQ snapshot. `maxOrNull` preserves the no-data state rather than fabricating an epoch date. Explicit date selection and existing per-date alert-count behavior remain unchanged.

After the fix, ClickHouse, the shared API, and the actual desktop Streamlit page agreed on snapshot `2026-10-11`, with 64 check results. The independent active incident queue still contained 17 incidents, including 10 critical incidents; zero failed checks in one historical daily snapshot does not mean the entire queue is resolved. No incident was removed to manufacture a healthy display.

The final 1,281-pixel desktop view retained English, the native theme, four KPI cards, the incident queue before detail/admin panels, and collapsed advanced settings. System keys and recovery controls remained out of the primary view. A private screenshot was retained outside the repository. This date/display correction made no external model call; personal Gemini testing in the API stayed enabled while the runner stayed disabled.

### Live language preference and cancellation recovery, 2026-10-13

The authenticated desktop client invoked `/dq language language:Indonesia`. DataSentry returned ephemeral confirmation `1559358645383340043`, and PostgreSQL held one `id` preference. The same actor then selected English through the command, received ephemeral confirmation `1559358853047648368`, and the durable preference became `en`. Neither command created a channel-visible response or called an LLM. Shared incident history and canonical thread `1559193662208868435` stayed unchanged. Private screenshots remain outside the repository.

A scoped source review found that cancelling the caller during a threaded SQL claim could strand a `claimed` record before any triage call. The runtime now shields and settles that claim, retaining `cancelled_before_start` history when appropriate. Repeated cancellation is handled during settlement; failed database cleanup remains fail-closed rather than allowing a speculative retry. A started provider call still becomes `uncertain` and cannot be automatically replayed.

Migration `07_discord_investigation_cancellation.sql` extends only the claim-status constraint; it was applied twice without deleting or changing existing claim rows. After deployment, the bot was ready with 22 guild commands and Message Content intent disabled. A second scoped review found no patch regression. The four real PostgreSQL regression cases prove durable cancellation/replay policy, not that two Discord users have participated. No extra Gemini call was needed for this fix or locale verification.

## Runtime flags and scope

After provider acceptance, `EXTERNAL_LLM_ENABLED=false` was restored for the runner. The API-only `COPILOT_EXTERNAL_LLM_ENABLED=true` override remains enabled per the owner's configuration. No provider call is implied by opening a persisted report. This documentation update makes no runtime, credential, Airflow, Discord, or provider changes.
