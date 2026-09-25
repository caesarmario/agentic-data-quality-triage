/**
 * Allowlisted same-origin BFF; approval credentials never reach the browser.
 * Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
 */

// --- Importing Libraries
import { NextRequest, NextResponse } from "next/server";
import {
  browserCapability,
  browserRequestError,
  readBoundedBody,
} from "@/lib/proxy-policy";

type RouteContext = { params: Promise<{ path: string[] }> };

// --- Defining Safe Error Responses
/** Return a cache-disabled JSON error without provider bodies or credentials. */
function jsonError(status: number, detail: string) {
  // Log only fixed diagnostic wording, never request bodies, keys, or headers.
  console.warn("Control-plane proxy rejected request", { status, detail });
  return NextResponse.json(
    { detail },
    { status, headers: { "cache-control": "no-store" } },
  );
}

// --- Forwarding Validated Requests
/** Validate browser capability and origin before any privileged upstream call. */
async function forward(request: NextRequest, context: RouteContext) {
  const { path: pieces } = await context.params;
  const capability = browserCapability(request.method, pieces ?? []);
  if (!capability)
    return jsonError(
      405,
      "This control-plane route is not exposed to the browser.",
    );
  const policyError = browserRequestError(
    request.method,
    request.headers,
    process.env.WEB_ORIGIN ?? "http://localhost:3000",
  );
  if (policyError)
    return jsonError(
      policyError,
      "Request rejected by browser origin or content policy.",
    );
  const baseUrl = process.env.CONTROL_PLANE_API_URL?.trim().replace(/\/$/, "");
  if (!baseUrl)
    return jsonError(
      503,
      "Control-plane API is not configured on this web server.",
    );
  const approvalToken = process.env.CONTROL_PLANE_APPROVAL_TOKEN?.trim();

  let body: string | undefined;
  if (request.method === "POST") {
    try {
      body = await readBoundedBody(request);
      const parsed: unknown = JSON.parse(body);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
        throw new SyntaxError();
    } catch (error) {
      return jsonError(
        error instanceof RangeError ? 413 : 400,
        "A bounded JSON object is required.",
      );
    }
  }

  if (capability === "approval" && !approvalToken) {
    return jsonError(
      503,
      "Approval mutations are not configured on this web server.",
    );
  }

  // Never forward caller authentication or routing headers to the private API.
  const headers = new Headers({ accept: "application/json" });
  if (body !== undefined) headers.set("content-type", "application/json");
  if (capability === "approval")
    headers.set("X-Control-Plane-Token", approvalToken!);
  try {
    const upstream = await fetch(
      `${baseUrl}/${pieces.join("/")}${request.nextUrl.search}`,
      {
        method: request.method,
        headers,
        body,
        cache: "no-store",
        redirect: "error",
        signal: AbortSignal.timeout(30_000),
      },
    );
    console.info("Control-plane proxy completed", {
      capability,
      status: upstream.status,
    });
    return new NextResponse(await upstream.text(), {
      status: upstream.status,
      headers: {
        "content-type": "application/json",
        "cache-control": "no-store",
      },
    });
  } catch {
    console.warn("Control-plane proxy unavailable", { capability });
    return jsonError(503, "Control-plane API is unavailable.");
  }
}

// --- Exposing Only Supported Methods
/** Forward an allowlisted read; the client cannot provide private credentials. */
export async function GET(request: NextRequest, context: RouteContext) {
  return forward(request, context);
}

/** Forward explicit triage or approval actions after same-origin validation. */
export async function POST(request: NextRequest, context: RouteContext) {
  return forward(request, context);
}
