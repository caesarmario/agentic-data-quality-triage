/**
 * Browser proxy policy for the control plane. No credentials or network access.
 * Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
 */

// --- Defining Explicit Browser Capabilities
const READ_PATHS = new Set([
  "api/v1/alerts",
  "api/v1/alerts/detail",
  "api/v1/summaries/daily",
  "api/v1/audit/logs",
  "api/v1/incidents/history",
  "api/v1/evidence/dq-history",
  "api/v1/evidence/pipeline-runs",
  "api/v1/approvals/requests",
  "api/v1/lineage/dbt",
  "api/v1/lineage/dbt/blast-radius",
  "api/v1/metadata/assets",
  "api/v1/reports/read",
]);
const APPROVAL_DETAIL =
  /^api\/v1\/approvals\/requests\/APR-[A-Za-z0-9_-]{1,100}$/;
const APPROVAL_ACTION =
  /^api\/v1\/approvals\/requests\/APR-[A-Za-z0-9_-]{1,100}\/(decision|cancel)$/;
const ASSET_DETAIL =
  /^api\/v1\/metadata\/assets\/[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$/;
export const MAX_REQUEST_BYTES = 65_536;
export type BrowserCapability = "read" | "triage" | "approval";

// --- Resolving Routes Without Prefix Expansion
/** Return a capability for an exact method/path, or null for unsafe/unknown paths. */
export function browserCapability(
  method: string,
  pieces: string[],
): BrowserCapability | null {
  if (
    !pieces.length ||
    pieces.some(
      (piece) =>
        piece === "." ||
        piece === ".." ||
        !/^[A-Za-z0-9._-]{1,200}$/.test(piece),
    )
  )
    return null;
  const path = pieces.join("/");
  if (
    method === "GET" &&
    (READ_PATHS.has(path) ||
      APPROVAL_DETAIL.test(path) ||
      ASSET_DETAIL.test(path))
  )
    return "read";
  if (method === "POST" && path === "api/v1/triage/run") return "triage";
  if (method === "POST" && APPROVAL_ACTION.test(path)) return "approval";
  return null;
}

// --- Enforcing Trusted Browser Origin
/**
 * Validate Host and same-origin JSON mutations against deployment configuration.
 * Args: HTTP method, incoming headers, configured public origin (not caller input).
 * Returns: HTTP rejection status or null. Forwarded headers are not trusted.
 */
export function browserRequestError(
  method: string,
  headers: Headers,
  configuredOrigin: string,
): number | null {
  let origin: URL;
  try {
    origin = new URL(configuredOrigin);
    if (
      !["http:", "https:"].includes(origin.protocol) ||
      origin.username ||
      origin.password ||
      origin.origin !== configuredOrigin
    )
      return 503;
  } catch {
    return 503;
  }
  if (headers.get("host") !== origin.host) return 403;
  if (headers.get("sec-fetch-site") === "cross-site") return 403;
  if (method === "POST") {
    if (headers.get("origin") !== origin.origin) return 403;
    if (
      headers.get("content-type")?.split(";")[0].trim().toLowerCase() !==
      "application/json"
    )
      return 415;
  }
  return null;
}

// --- Bounding Request Memory
/** Read bounded UTF-8 text from a Request; cancel oversized streams and raise RangeError. */
export async function readBoundedBody(request: Request): Promise<string> {
  const length = request.headers.get("content-length");
  if (length && (!/^\d+$/.test(length) || Number(length) > MAX_REQUEST_BYTES))
    throw new RangeError();
  const reader = request.body?.getReader();
  if (!reader) return "";
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_REQUEST_BYTES) {
        await reader.cancel();
        throw new RangeError();
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
}
