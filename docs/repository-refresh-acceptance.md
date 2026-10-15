# Repository Refresh Acceptance

## Publication scope

The owner approved publication of the existing local implementation and the documentation/media refresh to `origin/main`. The starting local and remote commit was `84274aea3290e24aeac31aab983d12e8fe74ec22`. No `master` branch is created and no force push is used.

The refresh changes presentation and offline validation, not runtime behavior, database schema or provider configuration. The previously implemented Discord workspace, Help Center and evidence-gated Resolve are included in the implementation snapshot. Proposed autonomous recovery/context-bundle work from discussion is not presented as an available feature.

The README is a short English entry point. The detailed setup and commands are retained in [Local operations](local-operations.md). [Architecture](architecture.md) and [Data model](data-model.md) distinguish actual stores, foreign keys, correlations and dbt derivations. [Video reproduction](demo-video.md) documents the recorded-UI walkthrough and its source provenance.

## Verification status

Pre-publication acceptance passed on **2026-10-15**. The GitHub attachment was uploaded and played in the authenticated README preview; final branch publication/read-back is a separate delivery step, reported with its commit IDs after push. This ledger does not claim that a local commit has already reached the remote.

### Airflow

- DAG: `91_dag_dq_platform_validation`, suite `all`, API required, web profile not required.
- Final run: `manual__repository_refresh_final_all_20261015T070052`, terminal state `success`.
- `t00_start`, `t10_run_named_pytest_suite`, `t20_run_platform_readiness`, `t30_emit_validation_summary`, and `t90_finish`: all `success`, attempt one.
- Retained pytest log: **2,069 passed, 12 skipped, 10 warnings**, 45.80 seconds. Skipped cases are not counted as passing evidence.
- Retained readiness log: **27 passed, 0 failed**, including the required API and platform dependencies.
- Existing warnings concern Python `audioop` and discord.py's legacy modal-label API; they were not suppressed.
- Initial run `manual__repository_refresh_all_20261015T065806` remains failed with five README-contract failures, 2,063 passed and 12 skipped. Its tests expected all operational examples in the root README. The repair tests the root plus its linked operations guide, retaining SQL engine, JSON safety, DAG, route, script and link validation. Old failed task logs were not cleared.

### Documents and diagrams

The 258-line README links to the complete operations guide. Offline checks validate local links/anchors, zero-key onboarding, source-backed DAG/API examples, safe JSON defaults and standalone SVG. Source generation checks reconcile **28 database objects**: 15 ClickHouse tables, one view and 12 PostgreSQL tables, with 399 catalog fields and five declared PostgreSQL foreign keys. The 18 generated artifacts are reproducible from DDL/migrations, dbt and diagram specs; no live database dump or runtime migration was used.

### Video and privacy

- [GitHub video attachment](https://github.com/user-attachments/assets/186c375d-3629-47e3-b9c8-b69b2e261a0b): native controls loaded a 180-second video and playback advanced with `paused=false` in README preview.
- Measured artifact: **7,452,064 bytes**, 1280x720, H.264/yuv420p, 30 fps, 5,400 frames, no audio stream.
- SHA-256: `b14ce2c69efd0cc38b1077b4daad9a303ae48f1c79fb4a18991bc6f0621778b8`.
- Local TypeScript and Node media checks: ten tests passed. ffprobe checks passed for format, duration, resolution, streams and upload size.
- Every chapter's first/middle/last frame was inspected via four private contact sheets; recorded Gemini source captures and the redacted closure card were also inspected directly.
- Public frames exclude unrelated Discord chrome and identities. The closure operator field has an explicit opaque redaction; incident evidence is not rewritten. Source hashes/crops and documentary summaries have separate provenance manifests.
- Candidate-source scanning found no concrete provider credentials or private corporate endpoints; the only token-pattern matches were explicit sanitizer test fixtures. Raw captures, dependency/cache directories, `.env`, auth files and TODO remain excluded.

The API personal-testing flag remains enabled, the runner flag remains disabled, and operational DAGs `00` and `90_04` remain paused. No new Gemini call or operational mutation was performed to prepare the documentation/media. These statements describe this refresh, not total account usage.

### Repository metadata and packaging

The approved concise description was saved and read back through the authenticated GitHub owner UI. Website, topics and repository settings were not edited. The README upload draft was cancelled without a browser commit; publication uses the local commits instead. GitHub preview provided native controls, loaded all 180 seconds and advanced playback.

The staged implementation was scanned without printing candidate credential values; no concrete key/private endpoint was found. Source-hashed diagrams and their inputs use repository-local LF attributes, with PNGs marked binary, so a Windows checkout does not silently change the recorded asset/source bytes. Isolated Remotion dependencies/caches are excluded from both Git and runtime Docker build contexts. No new runtime dependency was added.

## Safety and evidence rules

- No new paid model call, remediation, source regeneration, schema mutation or incident closure is performed for this refresh.
- API personal Gemini testing, runner kill switch and operational DAG pause states are preserved.
- The video uses actual retained UI captures and recorded reports. Its scenes span separate sessions and must not be interpreted as one autonomous repair.
- Screenshots and final video frames are reviewed for credentials and unrelated/private interface content before publication.
- Local `.env`, generated auth, logs, raw captures, render caches, dependencies and ignored TODO files are not publication inputs.
- Known local-POC execution, legacy coordination, publication and count-based coverage limitations are not repaired or hidden by documentation.

## Verification limits

This refresh does not establish a new fresh-clone installation, mobile/narrow acceptance, two-human isolation, simultaneous human decisions, production readiness or provider-account health. Existing dated ledgers retain those distinctions. Offline documentation checks and historical UI evidence are not substitutes for the final Airflow run or post-push GitHub inspection.
