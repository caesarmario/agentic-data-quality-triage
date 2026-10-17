import React from "react";
import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import { DailySummaryGate } from "../daily-summary-gate";
import type { DailySummary } from "../../lib/types";

function render(summary: DailySummary) {
  return renderToStaticMarkup(<DailySummaryGate summary={summary}>
    <span>Open alerts: {summary.total_open_alerts}</span>
  </DailySummaryGate>);
}
const empty: DailySummary = {dt:null,availability:"empty",check_counts:[],alert_counts:[],
  total_checks:0,total_open_alerts:0,duration_ms:0,summary:"No quality data yet"};

test("empty quality data is neutral and does not render healthy zero metrics", () => {
  const html=render(empty);
  assert.match(html,/No quality data yet/);
  assert.doesNotMatch(html,/Open alerts:|null|1970/);
});
test("alerts-only available response retains the open alert metric", () => {
  const html=render({...empty,dt:"2026-10-16",availability:"available",total_open_alerts:1,
    alert_counts:[{severity:"critical",count:1}]});
  assert.match(html,/Open alerts: 1/);
  assert.match(html,/Checks have not run/);
  assert.doesNotMatch(html,/No quality data yet/);
});
