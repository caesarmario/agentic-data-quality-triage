/**
 * Server-only FastAPI client. It deliberately never exposes the upstream URL
 * or approval token to browser code.
 * Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
 */
import "server-only";

export type ApiResult<T> = { data: T | null; error: string | null };

function apiBaseUrl(): string | null {
  const value = process.env.CONTROL_PLANE_API_URL?.trim();
  return value ? value.replace(/\/$/, "") : null;
}

export async function controlPlane<T>(path: string, init?: RequestInit): Promise<ApiResult<T>> {
  const baseUrl = apiBaseUrl();
  if (!baseUrl) return { data: null, error: "Control-plane API is not configured on this web server." };
  try {
    const response = await fetch(`${baseUrl}${path}`, { ...init, cache: "no-store", signal: AbortSignal.timeout(15_000) });
    if (!response.ok) return { data: null, error: `Control-plane request failed (${response.status}).` };
    return { data: (await response.json()) as T, error: null };
  } catch {
    return { data: null, error: "Control-plane API is unavailable. No local data is substituted." };
  }
}

export function query(path: string, params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => { if (value !== undefined && value !== null && value !== "") search.set(key, String(value)); });
  return `${path}?${search.toString()}`;
}
