# Premium Web UI

A Next.js operator interface for the Agentic Data Quality Triage control plane.

## Runtime Boundary

- Pages read only the existing FastAPI public contracts.
- Browser requests use a same-origin, allowlisted proxy; `CONTROL_PLANE_API_URL` and `CONTROL_PLANE_APPROVAL_TOKEN` remain server-only.
- Existing reports are read only from the selected alert's API-returned `report_s3_uri`, through the bounded `/api/v1/reports/read` endpoint. JSON fields are rendered only when the embedded report alert key matches the selected alert.
- Triage runs are explicit user actions. Rendering a page never invokes a model or creates a remediation request.
- Approval controls persist durable authorization only when the server-side approval token is configured.

## Alert Lifecycles

The Incident Center and Triage Workbench support `open`, `triaged`, and `resolved` filters. Their default is `triaged` so completed investigations remain discoverable when the open queue is empty. An explicit `?alert=<alert_key>` lookup is independent of the selected lifecycle filter.

## Local Setup

```bash
cd apps/web
cp .env.example .env.local
npm ci
npm run typecheck
npm run build
```

Set `CONTROL_PLANE_API_URL` to the existing FastAPI service URL. Never expose approval credentials through a `NEXT_PUBLIC_` variable.

## Validation

`npm run typecheck` validates the TypeScript contracts. `npm run build` produces the standalone production output. Final operational acceptance remains the parent-owned Airflow validation path.
