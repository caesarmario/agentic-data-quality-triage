/**
 * Human-readable dates and statuses without changing warehouse business dates.
 * Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
 */

// --- Defining Display Policy
export const DISPLAY_TIMEZONE = "Asia/Bangkok";
const MISSING = "Not recorded";

// --- Formatting Dates And Times
/** Return the Bangkok calendar date for an instant, independent of server timezone. */
export function businessDate(now: Date = new Date()): string {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: DISPLAY_TIMEZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const get = (type: string) => parts.find((part) => part.type === type)!.value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}

/** Format a date or timestamp; ISO business dates retain their literal calendar day. */
export function formatDate(value?: string | null): string {
  if (!value) return MISSING;
  const date = new Date(value);
  if (!Number.isFinite(date.valueOf())) return MISSING;
  return new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium",
    timeZone: /^\d{4}-\d{2}-\d{2}$/.test(value) ? "UTC" : DISPLAY_TIMEZONE,
  }).format(date);
}

/** Format an API timestamp in the project's explicit operator timezone. */
export function formatDateTime(value?: string | null): string {
  if (!value) return MISSING;
  const date = new Date(value);
  if (!Number.isFinite(date.valueOf())) return MISSING;
  return (
    new Intl.DateTimeFormat("en-GB", {
      dateStyle: "medium",
      timeStyle: "short",
      timeZone: DISPLAY_TIMEZONE,
    }).format(date) + " (Asia/Bangkok, UTC+07)"
  );
}

// --- Formatting Measured Values
/** Return a finite number with grouping, otherwise explicitly mark missing telemetry. */
export function formatNumber(value?: number | null): string {
  return value === null || value === undefined || !Number.isFinite(value)
    ? MISSING
    : new Intl.NumberFormat("en-GB").format(value);
}

/** Map exact known states to severity tokens; unknown states are never labelled healthy. */
export function statusTone(
  value: string,
): "danger" | "warning" | "success" | "neutral" {
  const normalized = value.toLowerCase();
  if (
    [
      "critical",
      "high",
      "fail",
      "failed",
      "error",
      "rejected",
      "reject",
      "blocked",
    ].includes(normalized)
  )
    return "danger";
  if (
    ["warning", "warn", "pending", "review", "medium", "partial"].includes(
      normalized,
    )
  )
    return "warning";
  if (
    [
      "pass",
      "passed",
      "success",
      "approved",
      "active",
      "complete",
      "completed",
      "resolved",
    ].includes(normalized)
  )
    return "success";
  return "neutral";
}
