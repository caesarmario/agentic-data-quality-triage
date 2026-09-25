/**
 * Incident Center supports the lifecycle selected in the URL while an explicit
 * alert detail lookup remains authoritative even if it is outside that filter.
 * Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
 */
import { AlertLifecycleLinks } from "@/components/alert-lifecycle-links";
import { PageHeader } from "@/components/page-header";
import { Badge, Card, Empty, Unavailable } from "@/components/ui/primitives";
import { controlPlane, query } from "@/lib/control-plane";
import { formatDateTime, statusTone } from "@/lib/format";
import type { Alert, AlertList, AuditLog, IncidentHistory } from "@/lib/types";

export const dynamic = "force-dynamic";
type Props = { searchParams: Promise<{ alert?: string; status?: string }> };
const lifecycle = new Set(["open", "triaged", "resolved"]);

export default async function IncidentCenter({ searchParams }: Props) {
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
  const [audit, history] = selected
    ? await Promise.all([
        controlPlane<AuditLog>(
          query("/api/v1/audit/logs", {
            alert_key: selected.alert_key,
            limit: 20,
          }),
        ),
        controlPlane<IncidentHistory>(
          query("/api/v1/incidents/history", {
            alert_reference: selected.alert_key,
            lookback_days: 90,
            limit: 10,
          }),
        ),
      ])
    : [
        { data: null, error: null },
        { data: null, error: null },
      ];

  return (
    <>
      <PageHeader
        eyebrow="Incident Center"
        title="Investigate durable outcomes"
        detail="Select any open, triaged, or resolved alert. An explicit alert reference is loaded independently from the visible lifecycle list."
      />
      <div className="split-layout">
        <Card className="alert-rail">
          <h2>Alert selection</h2>
          <AlertLifecycleLinks page="/incidents" selected={status} />
          {alerts.data ? (
            alerts.data.alerts.length ? (
              <div className="stack-list">
                {alerts.data.alerts.map((item) => (
                  <a
                    key={item.alert_key}
                    href={`/incidents?status=${status}&alert=${encodeURIComponent(item.alert_key)}`}
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
                detail="Use another lifecycle or an explicit alert URL to inspect a retained record."
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
                      Alert key:{" "}
                      <span className="mono">{selected.alert_key}</span>
                    </p>
                  </div>
                  <Badge tone={statusTone(selected.status)}>
                    {selected.status}
                  </Badge>
                </div>
                <p className="muted">
                  <span className="mono">{selected.table_name}</span> |{" "}
                  {selected.metric}
                </p>
              </Card>
              <Card>
                <div className="section-heading">
                  <div>
                    <h2>Audit trail</h2>
                    <p>Sanitized, append-only events for the selected alert.</p>
                  </div>
                  {audit.data && <Badge>{audit.data.row_count} events</Badge>}
                </div>
                {audit.data ? (
                  audit.data.rows.length ? (
                    <div className="timeline">
                      {audit.data.rows.map((row, index) => (
                        <div
                          className="timeline-row"
                          key={row.audit_id ?? `${row.action}-${index}`}
                        >
                          <span
                            className={`timeline-dot ${statusTone(row.status)}`}
                          />
                          <div>
                            <strong>{row.action}</strong>
                            <p>
                              {row.actor}
                              {row.tool_name ? ` via ${row.tool_name}` : ""}
                              {row.duration_ms !== null &&
                              row.duration_ms !== undefined
                                ? `, ${row.duration_ms} ms`
                                : ""}
                            </p>
                            {row.error_message && (
                              <p className="error-text">{row.error_message}</p>
                            )}
                          </div>
                          <time>{formatDateTime(row.ts)}</time>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <Empty
                      title="No audit events"
                      detail="No sanitized audit records were returned for this alert."
                    />
                  )
                ) : (
                  <Unavailable
                    message={audit.error ?? "Audit history unavailable."}
                  />
                )}
              </Card>
              <Card>
                <div className="section-heading">
                  <div>
                    <h2>Prior investigations</h2>
                    <p>
                      Durable outcomes without hidden prompts, SQL, or raw tool
                      output.
                    </p>
                  </div>
                  {history.data && (
                    <Badge>{history.data.row_count} records</Badge>
                  )}
                </div>
                {history.data ? (
                  history.data.rows.length ? (
                    <div className="stack-list">
                      {history.data.rows.map((item) => (
                        <article className="history-row" key={item.memory_id}>
                          <div className="badge-row">
                            <Badge tone={statusTone(item.outcome_status)}>
                              {item.outcome_status}
                            </Badge>
                            <Badge tone={statusTone(item.approval_state)}>
                              {item.approval_state}
                            </Badge>
                          </div>
                          <strong>
                            {item.top_hypothesis_category ||
                              "No hypothesis category recorded"}
                          </strong>
                          <p>
                            {item.summary || "No operator summary recorded."}
                          </p>
                          <small>
                            {formatDateTime(item.recorded_at)}
                            {item.confidence !== null &&
                            item.confidence !== undefined
                              ? ` | confidence ${item.confidence}`
                              : ""}
                          </small>
                        </article>
                      ))}
                    </div>
                  ) : (
                    <Empty
                      title="No prior investigations"
                      detail="The configured 90-day window has no retained outcomes for this alert."
                    />
                  )
                ) : (
                  <Unavailable
                    message={history.error ?? "Incident history unavailable."}
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
