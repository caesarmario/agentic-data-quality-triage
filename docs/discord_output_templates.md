####
## Discord Incident UX for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# Discord incident UX

Discord is an optional incident inbox and interaction surface. It is not the primary investigation workspace and it is not a replacement for Airflow, ClickHouse, or Streamlit.

Current workspace acceptance, including current Airflow runs, provider smoke evidence and unverified live checks, is tracked separately in [Discord workspace acceptance](discord_workspace_acceptance.md). The historical acceptance entries below are retained as historical evidence and do not imply that newer workspace interactions were live-verified.

All Discord output uses the existing green DataSentry bot identity. Slash commands use the bot gateway, while Airflow incident delivery and approval notifications use the Discord Bot REST API with the same bot token. There is no separate webhook sender or second DataSentry identity. The primary incident transport is one compact Discord embed; bounded plain Markdown remains a fallback when an embed cannot be sent.

## Channel contract

| Channel | Purpose |
| --- | --- |
| `dq-alerts` | Incident-only inbox: one lifecycle-aware card per data-quality incident; no summaries or platform startup messages |
| `dq-triage` | One durable English welcome; canonical public incident threads, explicit mention-help replies, alert lists and daily/weekly/table/warehouse summaries |
| `dq-ops-private` | HIGH-risk approval requests, approval decisions, and execution-boundary status that needs operator attention |

Bot startup diagnostics go to application logs only. `/dq debug` provides ephemeral runtime diagnostics when an operator needs them. Airflow health, container health, and unrelated platform alerts do not belong in any DQ incident channel. Summary commands publish their embeds to `dq-triage`; they never create cards in `dq-alerts`.

## Help Center and welcome

Use `/help`, `/dq help`, or a direct mention followed by `help` / `bantuan`. Slash help and help-navigation responses are private. Mention help is a short source-message reply; it does not create a question thread. Shared welcome clicks open private guidance in the clicking user's saved locale. Plain text `help` without a mention is ignored.

The guide uses deterministic embeds, never Gemini. Its footer is `System guide · No AI call`, or the curated Indonesian equivalent. This label describes the guide itself, not whether a preceding failed investigation incurred provider usage. Error cards use the separate `System feedback` footer.

Welcome outline, stored once in `dq-triage`:

```text
DataSentry - Start Here
I help you understand data-quality problems and choose a safe next step.

Find a problem
Open Show Incidents to see what needs attention.

Understand it
Pick an Alert Ref, then open Investigate.
Use Evidence for facts or Ask AI for an explanation.

Take action carefully
Approving a plan does not run it.
Resolving an incident does not repair its data.

[Get Started] [Commands] [Button Guide] [Safety & AI]
[Open Workspace, only when configured]
System guide · No AI call
```

The rendered title uses a small compass icon and green accent; neither an uptime indicator nor a claim of healthy data is included. The PostgreSQL service-message registry is separate from the incident registry. Creation is serialized, the delivery intent is committed before Discord I/O, and uncertain acknowledgements remain pending until reconciliation. Reconnect skips unchanged content. Old history is neither removed nor pinned.

Quick Start opens **Show Incidents** (private, `active`, maximum five) and explains the short `DQ-...` reference. **Commands** has three pages with actual registered inputs. **Button Guide** has two pages:

| Investigation | Meaning |
| --- | --- |
| Investigate | Open the saved report; start the first investigation only if none exists |
| Run New Investigation | Explicit new agent work; may use a provider |
| Evidence | Observed facts, not the model's hypothesis |
| Ask AI / Ask Follow-up | An incident-grounded question; may use a provider |
| View Report | Saved report and bounded attachments, not a new investigation |
| Open Workspace | More detail in Streamlit; localhost is local to that computer |
| Draft Action | A proposed plan/request, not data repair |

| Approval and closure | Meaning |
| --- | --- |
| View Plan | Read scope, risk, validation and rollback |
| Approve / Reject | Record a confirmed decision / reason; no execution |
| Resolve | Start checking whether this incident can close |
| Check Again | Scoped read-only Airflow check |
| Check Status | Progress of the same request |
| Close Incident | Enter a reason after fresh passing evidence |
| Confirm Close | Save closure only while every safety gate remains valid |
| Cancel | Leave the panel or cancel the available step; not a data rollback |

Guide navigation never includes executable approval, remediation or closure controls. **Safety & AI** explains that generated advice can be wrong, evidence is distinct from a hypothesis, approval is distinct from execution, and Resolve needs a fresh PASS, reason and confirmation.

For errors that reach the application, render two embeds in one private message:

```text
I couldn't use that input.
Please use a date like 2026-10-14.
Use the business date shown on the incident.
System feedback

Need a starting point?
Choose Show Incidents, copy an Alert Ref, then use the matching command.
Help does not approve, repair or close incidents.
[Show Incidents] [Commands] [Button Guide] [Safety & AI]
System guide · No AI call
```

Not-found, permission, backend and uncertain-result errors have separate bounded explanations. No exception class, raw argument, credential or provider body is included. Uncertain outcomes direct the user to the existing request rather than inviting an automatic retry. Unknown slash-command spelling is handled by Discord autocomplete, not a guaranteed DataSentry callback.

English is the default; personal pages use the user's saved preference. Help stays available with an English fallback if preferences or the data service are unavailable. Incident lookup still requires the shared service. Audit stores entry point, topic, actor/channel/message correlation and result, not question text or sensitive arguments. See [Help Center acceptance](discord_help_acceptance.md).

## Mention Ask in dq-triage

Type `@DataSentry`, select the real green bot in Discord autocomplete, and check that the mention is clickable. Then append the question below as an ordinary message, not a code block. Typing or pasting the bot name as plain text does not trigger the handler. The bot accepts direct human mentions only in the configured `dq-triage` channel or an existing DataSentry question thread.

```text
are there any active data-quality incidents?
```

No reaction means the question may not have reached admission: verify the actual mention, channel, and bot connection. Gemini being enabled in the API does not grant access to unmentioned message content. Use **Ask Follow-up** or `/dq ask` if selecting a mention is inconvenient.

A valid question receives an hourglass reaction while evidence is collected. After the answer is ready, DataSentry creates one public thread on the original message, mentions the requester, and sends the same compact Copilot embed used by slash commands. The source reaction becomes a checkmark after delivery, or a warning after failure. Neither reaction changes incident lifecycle or approval state.

Follow-up questions must mention DataSentry again inside that thread. They reuse the same thread and carry at most two verifiable visible exchanges (4,000 characters combined). The bot never scans the full channel or trusts dialogue as evidence. Questions can include one Alert Ref; otherwise the API supplies bounded active-alert context. Existing buttons continue to work in bot-authored public triage-thread messages; approval remains restricted to `dq-ops-private`.

Alternatively, **Ask Follow-up** opens a modal in a registered question thread. It shares mention admission limits and conversation context, records an atomic interaction claim, and publishes the answer in the same thread. Its small visible question field allows later follow-ups to retrieve that completed exchange. The model answers the latest question rather than repeating the starter summary. An opening question displayed at the top of a Discord thread is normal thread context, not evidence of a second bot reply.

Required additional triage permissions are Add Reactions, Create Public Threads, Send Messages in Threads, and Read Message History. Message Content intent stays disabled. There is no automatic replay of mentions sent while offline. Durable PostgreSQL source-message claims also prevent reconnect from repeating an uncertain paid request. Admission is bounded to three active questions, one per user/thread, and a ten-second user cooldown.

The footer reports the actual provider outcome. Mention Ask does not enable Gemini. Provider/API failure degrades without a second external attempt; technical usage and correlation remain in audit rather than the primary answer. See [acceptance evidence](discord_mention_acceptance.md).

## Incident card layout

The visible card contains only the facts needed to decide the next step:

```text
Raw Orders is empty for the selected business date

Severity / Status      CRITICAL | TRIAGED
Alert Ref              DQ-20260504-A1B2C3
Table / Date           dq.raw_orders | 2026-05-04
What changed           The selected partition contains no rows.
Why it matters         Downstream marts may be incomplete or stale.
AI assessment          The landing object or load stage needs verification.
Next best action       Inspect evidence before drafting a recovery action.

[Investigate] [Evidence] [Ask AI]
[Open Workspace] [Resolve]

AI analysis | Gemini 3.5 Flash Lite | Grounded in platform evidence
```
Severity, lifecycle, table, business date, Alert Ref, and approval state always come from deterministic system state. The model may explain the evidence and recommend a safe next step, but it cannot change those facts.

The card is compact and links the incident to its canonical public `dq-triage` thread rather than duplicating investigation output. `dq-alerts` contains one durable card per incident. Active cards use the two rows shown above; resolved cards expose **Evidence**, optional **Report** when available, and **Workspace**. **Investigate** reuses the latest persisted report. **Run New Investigation** is the explicit rerun action. A timeout or uncertain result is not grounds to retry an interaction automatically.

## Language and report artifacts

`/dq language language:English|Indonesia` persists a preference for the current guild and user. Shared cards and the starter message in an incident thread stay in English; personal Copilot answers use that preference. Streamlit's **English | Indonesia** setting is session-scoped. Raw reports, machine-readable fields and operator reasons are retained as submitted and are not translated.

Guarded report artifacts are limited to 200 KB. A report response identifies the authoritative JSON artifact URI and, when present, its Markdown sibling. It also returns copyable URI and bucket/key code blocks. An `s3://` URI is a storage locator, not a browser URL. Do not emit signed URLs or expose a public bucket. Opening **Report** is read-only and never triggers an investigation or provider call.

Narrative priority is:

1. `external_copilot` from a persisted, identity-matched provider-backed report.
2. `heuristic_copilot` from the bounded local Copilot.
3. `deterministic_fallback` when no trusted narrative is available.

The visible footer is derived from runtime evidence:

- External Gemini success: `AI analysis · Gemini 3.5 Flash Lite · Grounded in platform evidence`.
- Local heuristic route: `Heuristic analysis · No external model call`.
- Provider-disabled, malformed, or unavailable fallback: `System summary · External AI unavailable`.

The word Gemini is never shown for heuristic or deterministic output.

## Single-message lifecycle

`alert_key` is the durable idempotency key. The first Bot REST API create stores the returned bot-authored Discord message ID, transport, and author ID in `dq.discord_incident_messages`. Later lifecycle changes edit that same message through the Bot REST API. The sender validates that Discord returned a bot-authored message and records `transport=discord_bot_api`; it does not set a custom username or avatar.

Supported display states are:

- `OPEN`
- `TRIAGED`
- `WAITING APPROVAL`
- `APPROVED`
- `REJECTED`
- `RESOLVED`

An unchanged content hash skips the PATCH. A Discord `404` creates a replacement message and updates the registry. Retryable rate limits and transient failures use bounded retry. Initial creates include a stable Discord nonce with `enforce_nonce=true` so a retry does not intentionally create a second card. A notification failure is audited and does not roll back the DQ result, triage report, approval record, or alert lifecycle.

## Resolve with evidence

**Resolve closes a verified incident; it never repairs data.** The incident and related triage cards expose Resolve while active. A resolved card offers Evidence and optional Workspace only.

The operator opens an ephemeral panel, chooses **Check Again**, checks progress, and supplies a reason plus confirmation after a fresh PASS. DQ requires the exact incident scope and positive prerequisites. Schema drift requires a clean current contract comparison without warnings. Historical PASS, expired evidence, changed revision or outstanding remediation cannot authorize closure.

```text
Revalidation passed
Expected: 25     Observed: 25
The latest check did not find the incident condition in the validated scope.
[Confirm Resolve] [Evidence]

System check | No data repair or LLM call
```

The status card and closure use system-check provenance, never Gemini provenance. Final confirmation requires ops-channel access and any configured approver roles. A reason modal alone does not close an incident. PASS is usable for confirmation for at most ten minutes; when it expires before closure, choose Check Again explicitly. Once a valid decision is saved, later evidence expiry does not undo RESOLVED. A short-lived final confirmation panel disables its controls when it expires or finishes; reopen Resolve for current state rather than trusting an old panel.

For an active incident, Copilot may explain that an earlier PASS has expired and recommend **Resolve -> Check Again**. Do not use `/dq triage` as a substitute for the guarded revalidation-and-confirmation flow. For an already RESOLVED incident, Copilot explains the saved closure and recommends viewing Evidence or the report and continuing monitoring, not closing it again. Resolve requires fresh matching PASS evidence (at most ten minutes old), an operator reason and explicit confirmation; it does not call an LLM or repair data.

The durable incident message is updated to RESOLVED with verified result, operator, time and reason. Old AI analysis remains in history rather than describing the current condition. Synchronization warnings use the current backend preview, not the historical decision's initial outbox flag. See the [operator guide and acceptance ledger](incident_resolution.md).

## Commands

The interaction surface uses guild-scoped slash commands and does not inspect arbitrary server messages.

```text
/dq alerts dt:<optional YYYY-MM-DD> status:<active|open|acknowledged|triaged|resolved> limit:<n>
/dq daily_summary dt:<YYYY-MM-DD>
/dq weekly_summary end_date:<YYYY-MM-DD>
/dq table_summary table_name:<schema.table> start_date:<YYYY-MM-DD> end_date:<YYYY-MM-DD>
/dq db_summary start_date:<YYYY-MM-DD> end_date:<YYYY-MM-DD>
/dq triage alert_key:<Alert Ref or system key>
/dq language language:<English|Indonesia>
/dq ask question:<question> alert_key:<optional Alert Ref>
/dq debug alert_key:<Alert Ref or system key>
/dq backfill_preview start_date:<YYYY-MM-DD> end_date:<YYYY-MM-DD> target_dag_id:<DAG ID> reason:<reason>
/dq approve request_id:<APR ID> comment:<review note>
/dq reject request_id:<APR ID> comment:<review note>
```

`/dq debug` is ephemeral. It exposes sanitized correlation data such as the system alert key, agent run ID, report ID and URI, provider/model, estimated usage, and transport. These fields are not repeated in normal incident cards.

## Copilot answer

`/dq ask` returns the answer directly rather than wrapping every response in Question, Guardrail, Technical Reference, and Suggested Command sections.

```text
DQ Copilot

There are two active incidents. Start with DQ-20260504-A1B2C3 because it is
critical and affects dq.raw_orders. The other incident is a warning on the
curated daily mart. Select one Alert Ref before requesting root-cause analysis.

source=external_copilot | transport=api | runtime=gemini/gemini-3.5-flash-lite
```

An ungrounded platform question receives a bounded active-incident context. An incident-specific question should include an Alert Ref. Prior incident history is comparison context only; it does not prove the current root cause.

## Triage result

The triage embed prioritizes the explanation, evidence reasoning, uncertainty, confidence, and one safe recommendation:

```text
Triage | Raw Orders is empty for the selected business date

The latest partition is empty after the load stage. Evidence supports a missing
or unreadable landing object, but the stored report does not prove which condition occurred.

Confidence              82%
Alert Ref               DQ-20260504-A1B2C3
Evidence                Landing row count is zero; DQ check failed; load evidence is incomplete.
Still uncertain         The landing object has not been independently confirmed.
Recommended next step   Inspect the S3 object and load task before proposing a backfill.

[Ask Follow-up] [Evidence] [View Report] [Draft Action]

AI analysis | Gemini 3.5 Flash Lite | Grounded in platform evidence
```

The triage workflow may read guarded evidence and write reports. It cannot mutate warehouse data, execute arbitrary SQL, approve a request, or trigger remediation.

## Approval messages

Approval messages are posted to `dq-ops-private` when configured:

```text
Approval | APR-20260504-A1B2C3D4

The exact backfill scope is waiting for human review.

Status          WAITING APPROVAL
Risk            HIGH
Proposed scope  00_dag_dq_platform_daily_orchestrator | 2026-05-04
Validation plan Re-run DQ checks and verify the affected partition.
Recovery boundary No automatic rollback is implemented. A dispatched action requires a separate operator recovery decision and the documented Airflow runbook.

[View Plan] [Approve] [Reject]
```

`Approve` requires a confirmation step. `Reject` requires a reason. Both handlers validate guild/channel/message scope, acquire the PostgreSQL control-store transaction lock for the request, reload authoritative state, persist only one valid decision transition, write an audit event, and refresh the linked incident. Recording `APPROVED` still does not execute Airflow; the dispatcher remains a separate operator-controlled boundary. There is no automatic rollback. If a later dispatcher run starts work, recovery is a separate operator decision. The transitional `/dq approve` and `/dq reject` aliases open these controls instead of bypassing them.

## Permissions and safety

- OAuth scopes: `bot` and `applications.commands`.
- Required bot permissions: View Channels, Send Messages, and Embed Links.
- Optional permission: Read Message History.
- Privileged Message Content Intent stays disabled.
- All bot-authored message payloads suppress mentions.
- Persistent component IDs are versioned and bounded; handlers reload authoritative backend state rather than trusting button payload state.
- All users with access to the private operations channel may decide during the private demo. Set `DISCORD_APPROVER_ROLE_IDS` to enforce an explicit role allowlist.
- Bot tokens, provider keys, hidden prompts, and raw provider errors are never rendered or persisted.
- Embed fields are sanitized and bounded to Discord limits.

## Runtime ownership

- `apps/discord_bot/formatters.py` owns compact embeds and Markdown fallback.
- `apps/discord_bot/contracts.py` owns typed payload, message-state, and render contracts.
- `apps/discord_bot/registry.py` owns durable ClickHouse message identity.
- `apps/discord_bot/delivery.py` owns Bot REST API create, PATCH, hash-skip, legacy-message replacement, and `404` replacement behavior.
- `apps/discord_bot/bot.py` owns slash-command interaction and ephemeral debug output.
- `apps/discord_bot/service.py` reuses the shared control-plane API and deterministic tools.
- DAG 30 creates initial incident cards after deterministic alert generation.
- DAG 40 refreshes matching cards after triage.

The source of truth remains the authoritative platform stores and persisted report artifacts, not Discord message content. ClickHouse owns warehouse evidence, audit and lifecycle projections. The PostgreSQL control store owns transactional approvals, incident lifecycle, revalidation requests and human closure decisions.

## Implementation and acceptance status

For the current workspace-specific status, use the [acceptance ledger](discord_workspace_acceptance.md). It distinguishes Airflow-backed automated evidence, owner-operated interactions, delegated live browser closure, and two-user/mobile/recovery checks that remain pending.

Implemented controls in the current checkout:

- Bot REST API delivery with the single green DataSentry identity; retired webhook configuration is not part of the active transport.
- Incident-only `dq-alerts`, summary and investigation output in `dq-triage`, and approval state in `dq-ops-private`.
- Compact embeds, versioned persistent component IDs, authoritative state reload, HIGH approval risk, confirmation/rejection controls, and no direct remediation action.
- PostgreSQL control-store transaction locking for approval decisions and a separate Airflow execution boundary.
- Runtime-derived Gemini, heuristic, and deterministic provenance labels.

Fresh acceptance on 2026-10-08 covered the latest channel routing, approval lock, HIGH-risk recovery wording, Bot REST transport, and standalone DAG 40 budget enforcement:

- DAG 91 run `manual__discord_incident_decision_ux_20261008T060000`: all five tasks succeeded, 154 Discord-suite tests passed, and 20/20 readiness checks passed.
- DAG 91 run `manual__all_incident_decision_ux_20261008T060100`: all five tasks succeeded, 1,160 tests passed, 12 skipped, and 20/20 readiness checks passed.
- Final post-documentation DAG 91 run `manual__final_incident_decision_ux_20261008T060400`: all five tasks succeeded, 1,160 tests passed, 12 skipped, and 20/20 readiness checks passed.
- Daily parent run `manual__discord_incident_e2e_20261008T060300` and its landing, dbt, quality, and triage children all succeeded for the synthetic `duplicates_spike` scenario.
- DAG 30 created bot-authored message `1557526654618706023` as `OPEN`; DAG 40 updated that same message to `TRIAGED` through `discord_bot_api`.
- Strict DAG 92 run `manual__llm_smoke_discord_ux_20261008T055800` executed Gemini 3.5 Flash-Lite once with 127 input and 37 output tokens, USD 0.00013060 estimated cost, and no fallback.
- Paid DAG 40 run `manual__gemini_incident_ux_20261008T060000` completed exactly three Gemini calls with 8,727 aggregate tokens and USD 0.00540770 estimated cost. Its live card used `external_copilot` provenance and the Gemini footer.
- Fallback DAG 40 run `manual__fallback_incident_ux_20261008T060100` used zero external calls and zero provider cost, then updated the same message to `Heuristic analysis | No external model call` without changing deterministic incident facts.
- Discord Bot REST read-back confirmed author ID `1476800539747553463`, no `webhook_id`, compact embed fields, and the `Investigate`, `Ask AI`, `Evidence`, and `Open Workspace` actions.
- `EXTERNAL_LLM_ENABLED=false` and the paused daily DAG state were restored after acceptance.

Live component acceptance was completed on 2026-10-08 against incident `DQ-20261008-72DF3B`:

- `Investigate`, `Ask AI`, and `Evidence` succeeded from bot-authored incident message `1557526654618706023` in `dq-alerts`. `Open Workspace` resolved through Discord's outbound-link confirmation to `http://localhost:8501/`.
- `Ask Follow-up`, `Evidence`, `View Report`, and `Draft Action` succeeded from triage message `1557531739902050388` in `dq-triage`. The report remained an ephemeral persisted-artifact reference, and the draft created HIGH-risk request `APR-20261008-1CAA3926` without dispatching execution.
- `View Plan` succeeded for existing request `APR-20261006-A89DDA5E` and the new request. The first request was confirmed `APPROVED`; the second was reason-required `REJECTED` with the acceptance-only explanation retained in the control store.
- Authoritative ClickHouse state kept incident message `1557167370059915377` at `APPROVED` and message `1557526654618706023` at `REJECTED`, both through `discord_bot_api` and DataSentry author `1476800539747553463`.
- Both approval rows remained `execution_status=not_started` with empty execution DAG run IDs. The Airflow metadata database had zero new DagRuns during the decision window, ClickHouse had zero dispatch/remediation/mutation audit actions, and no pipeline run was created.
- Interaction audits retained actor, guild, channel, message, target, result, and alert correlation for Ask, Evidence, Report, Draft, View Plan, Approve, and Reject. Heuristic responses remained labeled as no external model calls.

The live Evidence click first exposed a Discord.py regression: read-only embeds passed `view=None` explicitly and failed safely with an audited `TypeError`. The response helpers now omit the `view` argument when no actions exist. Fresh Airflow DAG 91 run `manual__validation_discord_20261007T231620076600` passed all five tasks with 156 Discord-suite tests and 20/20 readiness checks after the repair, and the repeated live Evidence click succeeded.

Final post-live Airflow DAG 91 run `manual__validation_all_20261008T115703768198` passed all five tasks with 1,162 tests passed, 12 skipped, one `audioop` dependency deprecation warning, and 20/20 readiness checks.

Automated tests still provide serialization and fail-closed coverage; the evidence above adds the previously missing live Discord interaction proof.
