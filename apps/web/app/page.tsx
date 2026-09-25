/**
 * Reliability overview uses API aggregates and shows recent triaged/resolved
 * alerts so a healthy empty open queue does not hide completed investigations.
 * Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
 */
import { PageHeader } from "@/components/page-header";
import {
  Badge,
  Card,
  Empty,
  Metric,
  Unavailable,
} from "@/components/ui/primitives";
import { controlPlane, query } from "@/lib/control-plane";
import {
  businessDate,
  formatDate,
  formatDateTime,
  formatNumber,
  statusTone,
} from "@/lib/format";
import type { AlertList, DailySummary } from "@/lib/types";

export const dynamic = "force-dynamic";

function AlertWatchlist({
  alerts,
  title,
}: {
  alerts: AlertList;
  title: string;
}) {
  return (
    <Card>
      <div className="section-heading">
        <div>
          <h2>{title}</h2>
          <p>{alerts.summary}</p>
        </div>
        <a href="/incidents?status=triaged" className="text-link">
          Open Incident Center
        </a>
      </div>
      {alerts.alerts.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Reference</th>
                <th>Lifecycle</th>
                <th>Severity</th>
                <th>Asset</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {alerts.alerts.map((alert) => (
                <tr key={alert.alert_key}>
                  <td>
                    <a
                      className="table-link"
                      href={`/incidents?status=${encodeURIComponent(alert.status)}&alert=${encodeURIComponent(alert.alert_key)}`}
                    >
                      {alert.alert_display_id || alert.alert_key}
                    </a>
                  </td>
                  <td>
                    <Badge tone={statusTone(alert.status)}>
                      {alert.status}
                    </Badge>
                  </td>
                  <td>
                    <Badge tone={statusTone(alert.severity)}>
                      {alert.severity}
                    </Badge>
                  </td>
                  <td className="mono">{alert.table_name}</td>
                  <td>{formatDateTime(alert.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty
          title={`No ${title.toLowerCase()} returned`}
          detail="The control plane returned a valid empty result for this lifecycle filter."
        />
      )}
    </Card>
  );
}

export default async function ReliabilityOverview() {
  const dt = businessDate();
  const [summary, triaged, resolved] = await Promise.all([
    controlPlane<DailySummary>(query("/api/v1/summaries/daily", { dt })),
    controlPlane<AlertList>(
      query("/api/v1/alerts", { status: "triaged", limit: 8 }),
    ),
    controlPlane<AlertList>(
      query("/api/v1/alerts", { status: "resolved", limit: 8 }),
    ),
  ]);

  return (
    <>
      <PageHeader
        eyebrow="Reliability Overview"
        title="Daily quality posture"
        detail={`Evidence snapshot for ${formatDate(dt)}. Values are returned by the control plane, not calculated in the browser.`}
      />
      <div className="content-stack">
        {summary.data ? (
          <>
            <div className="metric-grid">
              <Metric
                label="Total checks"
                value={formatNumber(summary.data.total_checks)}
                detail={`For ${formatDate(summary.data.dt)}`}
              />
              <Metric
                label="Open alerts"
                value={formatNumber(summary.data.total_open_alerts)}
                detail="Current lifecycle status: open"
                tone={
                  summary.data.total_open_alerts > 0 ? "warning" : "success"
                }
              />
              <Metric
                label="Execution time"
                value={`${formatNumber(summary.data.duration_ms)} ms`}
                detail="Control-plane summary query"
              />
              <Metric
                label="Check outcomes"
                value={summary.data.check_counts.length}
                detail="Distinct statuses returned"
              />
            </div>
            <Card>
              <div className="section-heading">
                <div>
                  <h2>Quality check outcomes</h2>
                  <p>{summary.data.summary}</p>
                </div>
                <Badge>{formatDate(summary.data.dt)}</Badge>
              </div>
              {summary.data.check_counts.length ? (
                <div className="count-list">
                  {summary.data.check_counts.map((item) => (
                    <div key={item.status}>
                      <Badge tone={statusTone(item.status)}>
                        {item.status}
                      </Badge>
                      <strong>{formatNumber(item.count)}</strong>
                    </div>
                  ))}
                </div>
              ) : (
                <Empty
                  title="No check aggregates returned"
                  detail="The control plane returned a valid summary with no check-status groups."
                />
              )}
            </Card>
          </>
        ) : (
          <Unavailable message={summary.error ?? "No summary returned."} />
        )}
        {triaged.data ? (
          <AlertWatchlist alerts={triaged.data} title="Recent triaged alerts" />
        ) : (
          <Unavailable
            message={triaged.error ?? "Triaged alert list unavailable."}
          />
        )}
        {resolved.data ? (
          <AlertWatchlist
            alerts={resolved.data}
            title="Recent resolved alerts"
          />
        ) : (
          <Unavailable
            message={resolved.error ?? "Resolved alert list unavailable."}
          />
        )}
      </div>
    </>
  );
}
