# DataSentry Recorded Walkthrough

A 180-second silent documentary tour with burned-in English captions and a [WebVTT sidecar](../tools/demo-video/captions.en.vtt). The source combines **actual retained UI captures**, repository diagrams, and clearly labelled acceptance-record summaries. It is not a recording of one end-to-end autonomous repair.

This recording predates saved-source recovery. Its diagram assets are frozen in `tools/demo-video/recorded-diagrams/`; current architecture and table definitions are documented separately. The movie does not demonstrate the new recovery runbooks, their live approval/execution, or their acceptance results.

![DataSentry recorded walkthrough poster](images/demo-refresh-poster.png)

## Delivery

[Play the published video attachment](https://github.com/user-attachments/assets/186c375d-3629-47e3-b9c8-b69b2e261a0b). The MP4 is hosted as a GitHub attachment, not a public warehouse/report object. The README also provides a poster fallback.


- Composition: `DataSentryDemo`, 1280x720, 30 fps, 5,400 frames, exactly 180 seconds.
- Format: MP4, H.264, `yuv420p`, no audio stream, maximum 9,500,000 bytes.
- Local output: `data/acceptance/repository-refresh/datasentry-demo.mp4` (ignored; not committed).
- Poster: `docs/images/demo-refresh-poster.png`.
- Captions: `tools/demo-video/captions.en.vtt`; the same cue source is burned into every video frame.
- [Measured media verification](../tools/demo-video/media-verification.json) records actual bytes, duration, streams, dimensions, frame count and SHA-256.
- GitHub upload and README embedding are separate publication steps. No remote upload is performed by the rendering scripts.

## Chapters

| Time | Content | Evidence Boundary |
| --- | --- | --- |
| 00:00-00:20 | Problem: daily snapshot versus active incident queue | Recorded English Streamlit, historical snapshot; not current platform health |
| 00:20-00:45 | Actual pipeline and responsibility boundaries | Repository architecture, not an application screenshot |
| 00:45-01:15 | Discord incident inbox, Help and canonical workspace | Cropped bot cards only; bounded inbox and separate sessions |
| 01:15-01:50 | Streamlit light/dark, stored Gemini investigation and answer | Original recorded model wording; hypotheses stay uncertain; no new call |
| 01:50-02:10 | Lineage and data model | Repository model path and ERD; impact hypothesis is not proof of an outage |
| 02:10-02:45 | Approval versus execution; blocked, PASS and explicit Resolve | Separate acceptance records and recorded closure; no source repair implied |
| 02:45-03:00 | Local quickstart | Instructions only; no setup commands or platform acceptance executed for this video |

## Reproduce

Prerequisites: Node.js 22+ and npm. The authored build used Node.js `24.14.1`. All four Remotion packages are pinned to `4.0.535`; React and React DOM are pinned to `19.1.1`. `package-lock.json` retains exact dependency resolution. No dependency is added to the application runtime.

From the repository root:

```powershell
Set-Location tools/demo-video
npm.cmd ci --cache .cache/npm --no-fund --no-audit
npm.cmd run prepare:assets
npm.cmd run check
npm.cmd run frames
npm.cmd run render
npm.cmd run verify
```

Asset preparation stages only the selected sanitized PNGs and both repository SVG diagrams into ignored `.cache/public/`. It requires `docs/diagrams/architecture.svg` and `docs/diagrams/data-model-overview.svg`; missing diagrams fail the final build instead of substituting mockups. The Remotion API renderer uses a temporary local asset server and its internal headless Chromium to paint this composition only. It does not operate a user's browser, visit the platform, or start application services. Its first run may download the renderer binary. npm installation and binary downloads require network access; no provider account or API key is needed.

Remotion renders H.264 at a 350 kb/s target with a 390 kb/s maximum rate and a 780 kb buffer. The verifier rejects an artifact over **9.5 decimal MB**. Static holds and restrained chapter entrances preserve UI readability at this small upload budget. Fonts use local Georgia, Trebuchet MS and Consolas fallbacks; cross-platform font metrics may differ, so inspect frames when regenerating on a different OS.

### Optional Original Recropping

The sanitized committed assets and their original hashes are enough for ordinary regeneration. To reproduce crop/mask operations from owner-retained originals, provide the private directory only through an environment variable; no personal path is embedded in source or public manifests:

```powershell
# Set CAPTURE_ROOT locally to the owner-retained capture directory first.
node scripts/prepare-assets.mjs "--captures=$env:CAPTURE_ROOT"
```

Do not copy or commit the private originals. `--captures-only` is an intermediate preparation option while diagrams are unavailable, not a publishable final build. Full preparation and all checks must succeed before final rendering.

## Provenance And Privacy

### Stored Report Readback, 2026-10-15

The new investigation/recommendation crops show an existing report opened through Streamlit's guarded **Read latest report artifact** control, not Run triage or Ask Copilot. Report `RPT-85F4FEA2` belongs to `DQ-20261008-72DF3B`, business date `2026-10-08`, and records 93 duplicate records out of 2,919 rows. Its retained narrative names `external_model` via `gemini/gemini-3.5-flash-lite` on `triage_reasoning`. The narrative explicitly leaves the precise upstream/load-layer cause unconfirmed and recommends source-partition evidence review before remediation approval.

The object locator is `s3://dq-artifacts/agent-reports/dt=2026-10-08/report_id=RPT-85F4FEA2/alert_key_hash=6b01b3ca23cb/agent_run_id=49ad8c8a-09b1-4714-8ab7-81c3e3ffd399/report.md`. It is a private object reference, not a public browser link. Reading it did not generate a new report, call a provider or change incident state. The date above is the capture/readback date, not a new investigation timestamp.

[Asset provenance](../tools/demo-video/asset-provenance.json) records original filenames, SHA-256 hashes, crop rectangles, the single operator-field mask, public asset hashes and labels. Original Discord chrome, server lists, user avatars, usernames, request input and message composer are excluded. The resolved card's operator name is covered by a clearly labelled opaque redaction. No incident facts, dates, counts, recommendations or business verdicts are rewritten. There are no mockups, generated UI or reconstructed product screenshots.

The investigation crop explicitly retains **Still uncertain**. Its report ID is not inferred from another session. Recorded Gemini provenance is historical, not a claim about currently available models, current pricing or a new call made during rendering.

[Documentary evidence mapping](../tools/demo-video/evidence-manifest.json) traces the business-verdict and approval summaries to:

- [Gemini triage acceptance](gemini_triage_acceptance.md): provider provenance and approval with execution still `not_started`.
- [Discord workspace acceptance](discord_workspace_acceptance.md): stored answers, original closure, snapshot/queue separation and desktop scope.
- [Incident resolution](incident_resolution.md): retained negative and positive revalidation run IDs, freshness gates and separate source preparation.

Editorial acceptance-record panels are labelled as summaries, not captured UI. Historical blocked, PASS and resolved evidence spans separate runs. Resolve verifies and records closure; it does not perform ingestion, backfill, dbt execution, schema repair or LLM analysis.

## Validation Scope

`npm.cmd run check` performs TypeScript checking and focused local Node source/asset tests. `npm.cmd run frames` samples each chapter's first, middle and last frame, plus every scene midpoint, and writes the poster. QA frames remain in ignored `.cache/qa-frames/`; the selected frame indices are recorded in `.cache/qa-frames.json`. Inspect those frames for text clipping, evidence readability, diagram rendering, caption coverage and privacy before publication.

`npm.cmd run verify` measures the final MP4 using ffprobe and enforces duration, 1280x720, 30 fps, 5,400 frames, H.264, `yuv420p`, no audio and the size ceiling. It records no local absolute input path.

These are **local media/source checks**, not a new application acceptance run. No Airflow DagRun is created or inspected by this media task, so it makes no new platform acceptance claim. The application evidence remains the named historical runs in the linked ledgers. Mobile/narrow views, two-human race/isolation and production readiness are not established by desktop footage. A separate fresh DAG 91 run is still required for acceptance of application changes; direct local tests and this video cannot substitute for it.
