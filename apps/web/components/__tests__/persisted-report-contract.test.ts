/**
 * Contract tests for the bounded persisted-report parser.
 * Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
 */
import assert from "node:assert/strict";
import test from "node:test";
import { parsePersistedTriageReport, resolvePersistedJsonReportUri } from "../../lib/types";
import { businessDate, formatDate, formatDateTime, formatNumber, statusTone } from "../../lib/format";

test("operator dates roll over at Bangkok midnight without shifting business dates", () => {
  assert.equal(businessDate(new Date("2026-09-07T16:59:59Z")), "2026-09-07");
  assert.equal(businessDate(new Date("2026-09-07T17:00:00Z")), "2026-09-08");
  assert.equal(formatDate("2026-09-07"), "7 Sept 2026");
  assert.match(formatDateTime("2026-09-07T18:00:00Z"), /8 Sept 2026.*01:00.*Asia\/Bangkok/);
  assert.equal(formatDateTime("invalid"), "Not recorded");
  assert.equal(formatNumber(Number.NaN), "Not recorded");
});

test("unknown status substrings cannot be presented as healthy", () => {
  assert.equal(statusTone("inactive"), "neutral");
  assert.equal(statusTone("unapproved"), "neutral");
  assert.equal(statusTone("resolved"), "success");
  assert.equal(statusTone("partial"), "warning");
  assert.equal(statusTone("high"), "danger");
});

test("stored Markdown alert reference resolves to its paired JSON artifact", () => {
  const prefix = "s3://dq-artifacts/agent-reports/dt=2026-06-10/report_id=RPT-123/agent_run_id=abc";
  assert.equal(resolvePersistedJsonReportUri(`${prefix}/report.md`), `${prefix}/report.json`);
  assert.equal(resolvePersistedJsonReportUri(`${prefix}/report.json`), `${prefix}/report.json`);
  for (const value of ["https://attacker.invalid/report.md", `${prefix}/../report.md`, `${prefix}/report.md?key=x`, `${prefix}/other.md`]) {
    assert.equal(resolvePersistedJsonReportUri(value), null);
  }
});

const report = JSON.stringify({
  alert: { alert_key: "dq|orders|2026-09-07" },
  report_id: "RPT-90F9F926",
  summary: "A bounded report.",
  impact: "Affected mart remains incomplete.",
  confidence: 0.82,
  hypotheses: [
    {
      hypothesis_id: "h-1",
      title: "Late arrival",
      description: "Source arrived after the check.",
      confidence: 0.82,
      root_cause_category: "late_arriving",
      recommended_action: "Verify arrival.",
      framing_source: "deterministic",
    },
  ],
  evidence: [
    {
      evidence_id: "e-1",
      evidence_type: "dq_history",
      tool_name: "dq_history",
      summary: "One failure.",
      row_count: 1,
      query: "must not surface",
    },
  ],
  llm_runtime: {
    route_event_count: 3,
    requested_routes: ["triage_reasoning"],
    executed_routes: ["triage_reasoning"],
    providers: ["gemini"],
    models: ["gemini-test"],
    external_model_used: true,
    heuristic_fallback_used: false,
    input_tokens: 30,
    output_tokens: 12,
    estimated_cost_usd: 0.0012,
    duration_ms: 1234,
    fallback_reasons: [],
  },
});

test("parses an allowlisted report matching the selected alert", () => {
  const parsed = parsePersistedTriageReport(report, "dq|orders|2026-09-07");
  assert.ok(parsed);
  assert.equal(parsed.report_id, "RPT-90F9F926");
  assert.equal(parsed.hypotheses[0]?.title, "Late arrival");
  assert.equal(parsed.evidence[0]?.tool_name, "dq_history");
  assert.equal(parsed.llm_runtime.input_tokens, 30);
});

test("rejects malformed and mismatched report artifacts", () => {
  assert.equal(
    parsePersistedTriageReport("not json", "dq|orders|2026-09-07"),
    null,
  );
  assert.equal(parsePersistedTriageReport(report, "dq|other|2026-09-07"), null);
});
