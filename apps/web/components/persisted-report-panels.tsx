/**
 * Renders the allowlisted public facts retained in a persisted triage report.
 * Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
 */
import { Badge, Card, Empty, Metric } from "@/components/ui/primitives";
import { formatNumber, statusTone } from "@/lib/format";
import type { PersistedTriageReport } from "@/lib/types";

function formatCost(value: number | null): string {
  return value === null ? "Not recorded" : `$${value.toFixed(6)}`;
}

export function PersistedReportPanels({
  report,
}: {
  report: PersistedTriageReport;
}) {
  const runtime = report.llm_runtime;
  const totalTokens =
    runtime.input_tokens === null || runtime.output_tokens === null
      ? null
      : runtime.input_tokens + runtime.output_tokens;

  return (
    <>
      <Card>
        <div className="section-heading">
          <div>
            <h2>Persisted triage report</h2>
            <p>
              Report {report.report_id || "identifier not recorded"}. This is an
              existing artifact read, not a new model invocation.
            </p>
          </div>
          {report.confidence !== null && (
            <Badge>{`${Math.round(report.confidence * 100)}% confidence`}</Badge>
          )}
        </div>
        <div className="stack-list">
          <article className="history-row">
            <strong>Executive summary</strong>
            <p>{report.summary || "No summary was retained in this report."}</p>
          </article>
          <article className="history-row">
            <strong>Impact</strong>
            <p>
              {report.impact ||
                "No impact statement was retained in this report."}
            </p>
          </article>
        </div>
      </Card>

      <div className="metric-grid">
        <Metric
          label="Route events"
          value={formatNumber(runtime.route_event_count)}
          detail="Recorded LLM route events"
        />
        <Metric
          label="Token usage"
          value={formatNumber(totalTokens)}
          detail="Derived from reported input and output totals"
        />
        <Metric
          label="Estimated cost"
          value={formatCost(runtime.estimated_cost_usd)}
          detail="Aggregate retained by report"
        />
        <Metric
          label="Route duration"
          value={
            runtime.duration_ms === null
              ? "Not recorded"
              : `${formatNumber(runtime.duration_ms)} ms`
          }
          detail="Aggregate retained by report"
        />
      </div>

      <Card>
        <div className="section-heading">
          <div>
            <h2>AI runtime</h2>
            <p>
              Aggregate telemetry retained by the report, separate from
              deterministic incident evidence.
            </p>
          </div>
        </div>
        <div className="stack-list">
          <article className="history-row">
            <strong>Providers and models</strong>
            <p>
              {runtime.providers.length
                ? runtime.providers.join(", ")
                : "No provider recorded"}{" "}
              /{" "}
              {runtime.models.length
                ? runtime.models.join(", ")
                : "No model recorded"}
            </p>
          </article>
          <article className="history-row">
            <strong>Executed routes</strong>
            <p>
              {runtime.executed_routes.length
                ? runtime.executed_routes.join(", ")
                : "No executed route recorded"}
            </p>
          </article>
          <article className="history-row">
            <strong>Runtime mode</strong>
            <p>
              External model used: {String(runtime.external_model_used)}.
              Heuristic fallback used: {String(runtime.heuristic_fallback_used)}
              .
            </p>
          </article>
          {runtime.fallback_reasons.length > 0 && (
            <article className="history-row">
              <strong>Fallback reasons</strong>
              <p>{runtime.fallback_reasons.join(", ")}</p>
            </article>
          )}
        </div>
      </Card>

      <Card>
        <div className="section-heading">
          <div>
            <h2>Ranked hypotheses</h2>
            <p>Candidate causes stored with this report.</p>
          </div>
          <Badge>{report.hypotheses.length} retained</Badge>
        </div>
        {report.hypotheses.length ? (
          <div className="stack-list">
            {report.hypotheses.map((item, index) => (
              <article
                className="history-row"
                key={item.hypothesis_id || `${item.title}-${index}`}
              >
                <div className="badge-row">
                  <Badge tone={statusTone(item.root_cause_category)}>
                    {item.root_cause_category || "uncategorized"}
                  </Badge>
                  <Badge>{item.framing_source || "source not recorded"}</Badge>
                </div>
                <strong>{item.title || "Untitled hypothesis"}</strong>
                <p>{item.description || "No explanation retained."}</p>
                <p>
                  <b>Recommended action:</b>{" "}
                  {item.recommended_action || "No action retained."}
                </p>
                {item.confidence !== null && (
                  <small>
                    Confidence: {Math.round(item.confidence * 100)}%
                  </small>
                )}
              </article>
            ))}
          </div>
        ) : (
          <Empty
            title="No hypotheses retained"
            detail="The report did not expose a ranked hypothesis collection."
          />
        )}
      </Card>

      <Card>
        <div className="section-heading">
          <div>
            <h2>Evidence reviewed</h2>
            <p>
              Summaries and counts only; raw tool output is intentionally not
              rendered.
            </p>
          </div>
          <Badge>{report.evidence.length} retained</Badge>
        </div>
        {report.evidence.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Tool</th>
                  <th>Evidence type</th>
                  <th>Rows</th>
                  <th>Summary</th>
                </tr>
              </thead>
              <tbody>
                {report.evidence.map((item, index) => (
                  <tr key={item.evidence_id || `${item.tool_name}-${index}`}>
                    <td className="mono">{item.tool_name || "Not recorded"}</td>
                    <td>{item.evidence_type || "Not recorded"}</td>
                    <td>{formatNumber(item.row_count)}</td>
                    <td>{item.summary || "No summary retained."}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty
            title="No evidence retained"
            detail="The report did not expose evidence summaries."
          />
        )}
      </Card>
    </>
  );
}
