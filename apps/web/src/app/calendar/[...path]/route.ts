import type { NextRequest } from "next/server";

import {
  isSelfReferentialOrigin,
  resolveApiOrigin,
  resolveInternalApiSecret,
} from "../../api/[...path]/route";

/**
 * The private ICS feed lives on the API, but the subscription URL is built from
 * CALENDAR_PUBLIC_BASE_URL -- the Web origin. Without this passthrough the
 * Settings page would hand out an address that 404s on the very origin it
 * points at, because nothing else forwards `/calendar/*` to the API.
 *
 * Deliberately NOT behind `isPublicApiAllowed`: that guard exists to stop the
 * public read-only link from performing writes, and this route is a GET-only
 * feed. The API already exempts `/calendar/*` from the shared-secret gate
 * (INTERNAL_API_SECRET_EXEMPT_PREFIXES) because calendar clients cannot send
 * custom headers; the unguessable path token is what protects the feed. We
 * still attach the secret when one is configured so the route keeps working if
 * that exemption is ever narrowed.
 *
 * The Cloudflare VPC binding is intentionally not consulted here: it is removed
 * from wrangler.jsonc for this deployment, so `INTERNAL_API_URL` is the only
 * way the API is reached. If the binding returns, mirror the branch in
 * `api/[...path]/route.ts`.
 */

function apiUnavailable(message: string) {
  return Response.json(
    { error: { code: "api_unavailable", message } },
    { status: 503, headers: { "Cache-Control": "no-store" } },
  );
}

async function proxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const { path } = await context.params;
  const apiOrigin = resolveApiOrigin();
  if (!apiOrigin) {
    return apiUnavailable("事件服务尚未连接，请配置 INTERNAL_API_URL");
  }
  if (isSelfReferentialOrigin(apiOrigin, request.nextUrl.origin)) {
    return apiUnavailable("事件服务地址不能指向当前 Web Worker，请配置独立 API 地址");
  }
  const target = new URL(`/calendar/${path.join("/")}`, apiOrigin);
  target.search = request.nextUrl.search;
  const headers = new Headers();
  const accept = request.headers.get("accept");
  if (accept) headers.set("accept", accept);
  const internalApiSecret = resolveInternalApiSecret();
  if (internalApiSecret) headers.set("X-Internal-Api-Secret", internalApiSecret);
  let upstream: Response;
  try {
    upstream = await fetch(
      new Request(target, { method: request.method, headers, redirect: "manual" }),
      { cache: "no-store" },
    );
  } catch {
    return apiUnavailable("事件服务暂时不可达，请检查后端或 Cloudflare Tunnel");
  }
  const responseHeaders = new Headers(upstream.headers);
  responseHeaders.delete("content-encoding");
  responseHeaders.delete("content-length");
  return new Response(upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

export const GET = proxy;
export const HEAD = proxy;
