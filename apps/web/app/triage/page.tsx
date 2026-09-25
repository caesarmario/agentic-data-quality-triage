/**
 * Triage Workbench reads existing, bounded report artifacts for selected
 * triaged/resolved alerts. It never starts a model run while rendering.
 * Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
 */
import { AlertLifecycleLinks } from "@/components/alert-lifecycle-links";
import { PageHeader } from "@/components/page-header";
import { PersistedReportPanels } from "@/components/persisted-report-panels";
import { TriageRunner } from "@/components/triage-runner";
import { Badge, Card, Empty, Unavailable } from "@/components/ui/primitives";
import { controlPlane, query } from "@/lib/control-plane";
import { formatDate, formatNumber, statusTone } from "@/lib/format";
import {
  parsePersistedTriageReport,
  resolvePersistedJsonReportUri,
  type Alert,
  type AlertList,
  type DqHistory,
  type PipelineRuns,
  type ReportArtifact,
} from "@/lib/types";

export const dynamic = "force-dynamic";
type Props = { searchParams: Promise<{ alert?: string; status?: string }> };
const lifecycle = new Set(["open", "triaged", "resolved"]);

function ReportArtifactState({
  artifact,
  selectedAlertKey,
}: {
  artifact: { data: ReportArtifact | null; error: string | null } | null;
  selectedAlertKey: string;
}) {
  if (!artifact)
    return (
      <Card>
        <Empty
          title="No persisted report reference"
          detail="This alert has no report artifact URI. You can still read deterministic evidence or explicitly run triage."
        />
      </Card>
    );
  if (!artifact.data)
    return (
      <Unavailable
        message={artifact.error ?? "Persisted report artifact is unavailable."}
      />
    );
  if (artifact.data.truncated)
    return (
      <Unavailable message="The persisted report exceeded the safe read limit and was not rendered." />
    );
  if (artifact.data.media_type !== "application/json")
    return (
      <Unavailable message="The selected report is not a JSON triage artifact, so structured report facts are unavailable." />
    );
  const report = parsePersistedTriageReport(
    artifact.data.text,
    selectedAlertKey,
  );
  return report ? (
    <PersistedReportPanels report={report} />
  ) : (
    <Unavailable message="The JSON report was malformed, did not match the selected alert, or lacked the public report contract." />
  );
}

export default async function TriageWorkbench({ searchParams }: Props) {
  const { alert: requested, status: requestedStatus } = await searchParams;
  const status = lifecycle.has(requestedStatus ?? "")
    ? requestedStatus!
    : "triaged";
  const alerts = await controlPlane<AlertList>(
    query("/api/v1/alerts", { status, limit: 50 }),
  );
  const selectedDetail = requested
    ? await controlPlane<Alert>(
        query("/api/v1/alerts/detail", { alert_key: requested }),
      )
    : null;
  // An explicit lookup must never silently inspect a different alert.
  const selected = requested
    ? selectedDetail?.data
    : alerts.data?.alerts[0];
  const jsonReportUri = selected?.report_s3_uri
    ? resolvePersistedJsonReportUri(selected.report_s3_uri)
    : null;
  const [dq, pipeline, reportArtifact] = selected
    ? await Promise.all([
        selected.dt
          ? controlPlane<DqHistory>(
              query("/api/v1/evidence/dq-history", {
                table_name: selected.table_name,
                dt: selected.dt,
                check_name: selected.metric,
                lookback_days: 14,
                limit: 20,
              }),
            )
          : Promise.resolve({
              data: null,
              error: "This alert has no business date for DQ history.",
            }),
        selected.dt
          ? controlPlane<PipelineRuns>(
              query("/api/v1/evidence/pipeline-runs", {
                dt: selected.dt,
                lookback_days: 7,
                limit: 20,
              }),
            )
          : Promise.resolve({
              data: null,
              error: "This alert has no business date for pipeline evidence.",
            }),
        jsonReportUri
          ? controlPlane<ReportArtifact>(
              query("/api/v1/reports/read", {
                s3_uri: jsonReportUri,
                max_bytes: 160000,
              }),
            )
          : Promise.resolve({ data: null, error: null }),
      ])
    : [{ data: null, error: null }, { data: null, error: null }, null];

  return (
    <>
      <PageHeader
        eyebrow="Triage Workbench"
        title="Read evidence before running triage"
        detail="Existing report artifacts load from the API only when selected. Running a new triage remains an explicit action and approval stays separate."
      />
      <div className="split-layout">
        <Card className="alert-rail">
          <h2>Alert selection</h2>
          <AlertLifecycleLinks page="/triage" selected={status} />
          {alerts.data ? (
            alerts.data.alerts.length ? (
              <div className="stack-list">
                {alerts.data.alerts.map((item) => (
                  <a
                    key={item.alert_key}
                    href={`/triage?status=${status}&alert=${encodeURIComponent(item.alert_key)}`}
                    className={
                      selected?.alert_key === item.alert_key
                        ? "alert-row selected"
                        : "alert-row"
                    }
                  >
                    <Badge tone={statusTone(item.status)}>{item.status}</Badge>
                    <strong>{item.alert_display_id || item.alert_key}</strong>
                    <span className="mono">{item.table_name}</span>
                    <small>{item.metric}</small>
                  </a>
                ))}
              </div>
            ) : (
              <Empty
                title={`No ${status} alerts`}
                detail="Use another lifecycle or an explicit alert URL to inspect a persisted report."
              />
            )
          ) : (
            <Unavailable message={alerts.error ?? "Alert list unavailable."} />
          )}
        </Card>
        <div className="content-stack">
        {requested && !selected ? (
          <Card>
            <Empty
              title="Requested alert unavailable"
              detail={
                selectedDetail?.error ??
                "The explicitly selected alert was not found."
              }
            />
          </Card>
        ) : selected ? (
            <>
              <Card>
                <div className="section-heading">
                  <div>
                    <h2>{selected.alert_display_id || selected.alert_key}</h2>
                    <p>
                      <span className="mono">{selected.table_name}</span> |{" "}
                      {selected.metric} | business date{" "}
                      {formatDate(selected.dt)}
                    </p>
                  </div>
                  <div className="badge-row">
                    <Badge tone={statusTone(selected.status)}>
                      {selected.status}
                    </Badge>
                    <Badge tone={statusTone(selected.severity)}>
                      {selected.severity}
                    </Badge>
                  </div>
                </div>
                <div className="evidence-grid">
                  <div>
                    <span>Observed</span>
                    <strong>{formatNumber(selected.observed_value)}</strong>
                  </div>
                  <div>
                    <span>Expected</span>
                    <strong>{formatNumber(selected.expected_value)}</strong>
                  </div>
                  <div>
                    <span>Threshold</span>
                    <strong>{formatNumber(selected.threshold_value)}</strong>
                  </div>
                </div>
                <TriageRunner alertKey={selected.alert_key} />
              </Card>
              <ReportArtifactState
                artifact={reportArtifact}
                selectedAlertKey={selected.alert_key}
              />
              <Card>
                <div className="section-heading">
                  <div>
                    <h2>DQ history</h2>
                    <p>
                      Bounded 14-day evidence window for the affected check.
                    </p>
                  </div>
                  {dq.data && <Badge>{dq.data.row_count} rows</Badge>}
                </div>
                {dq.data ? (
                  dq.data.rows.length ? (
                    <div className="table-wrap">
                      <table>
                        <thead>
                          <tr>
                            <th>Check</th>
                            <th>Status</th>
                            <th>Observed</th>
                            <th>Expected</th>
                          </tr>
                        </thead>
                        <tbody>
                          {dq.data.rows.map((row, index) => (
                            <tr
                              key={
                                row.check_run_id ?? `${row.check_name}-${index}`
                              }
                            >
                              <td>{row.check_name}</td>
                              <td>
                                <Badge tone={statusTone(row.status)}>
                                  {row.status}
                                </Badge>
                              </td>
                              <td>{formatNumber(row.observed_value)}</td>
                              <td>{formatNumber(row.expected_value)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <Empty
                      title="No DQ history"
                      detail="The API returned no rows in the selected evidence window."
                    />
                  )
                ) : (
                  <Unavailable
                    message={dq.error ?? "DQ history unavailable."}
                  />
                )}
              </Card>
              <Card>
                <div className="section-heading">
                  <div>
                    <h2>Pipeline evidence</h2>
                    <p>
                      Runs around the selected business date; this is evidence,
                      not an inferred cause.
                    </p>
                  </div>
                  {pipeline.data && (
                    <Badge>{pipeline.data.row_count} rows</Badge>
                  )}
                </div>
                {pipeline.data ? (
                  pipeline.data.rows.length ? (
                    <div className="table-wrap">
                      <table>
                        <thead>
                          <tr>
                            <th>Job</th>
                            <th>Airflow context</th>
                            <th>Status</th>
                            <th>Duration</th>
                          </tr>
                        </thead>
                        <tbody>
                          {pipeline.data.rows.map((row) => (
                            <tr key={row.run_id}>
                              <td>{row.job_name}</td>
                              <td className="mono">
                                {row.dag_id || row.task_id || "Not recorded"}
                              </td>
                              <td>
                                <Badge tone={statusTone(row.status)}>
                                  {row.status}
                                </Badge>
                              </td>
                              <td>
                                {row.duration_ms === null ||
                                row.duration_ms === undefined
                                  ? "Not recorded"
                                  : `${row.duration_ms} ms`}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <Empty
                      title="No pipeline evidence"
                      detail="The API returned no pipeline rows in the selected window."
                    />
                  )
                ) : (
                  <Unavailable
                    message={pipeline.error ?? "Pipeline evidence unavailable."}
                  />
                )}
              </Card>
            </>
          ) : (
            <Card>
              <Empty
                title="Select an alert"
                detail="No alert is available for this lifecycle. Use another lifecycle filter or add an explicit alert reference."
              />
            </Card>
          )}
        </div>
      </div>
    </>
  );
}
