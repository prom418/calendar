import { getCloudflareContext } from "@opennextjs/cloudflare";
import type { NextRequest } from "next/server";

const DEVELOPMENT_API_ORIGIN = "http://127.0.0.1:8000";
const VPC_API_ORIGIN = "http://api:8000";
const FORWARDED_REQUEST_HEADERS = [
  "accept",
  "accept-language",
  "content-type",
  "idempotency-key",
  "x-csrf-token",
  "x-request-id",
] as const;

/**
 * Hosts that only ever make sense when the API runs beside the Web process.
 *
 * A deployed Worker must never dial these. Cloudflare's edge refuses to connect
 * to a loopback address and answers `403` with the body `error code: 1003`, and
 * because that response is `text/plain` the proxy cannot classify it as an
 * Access rejection -- it reaches the browser as a bare 403 that looks like an
 * application bug instead of the honest "事件服务尚未连接" the UI expects.
 *
 * This is not hypothetical: OpenNext bakes the project's `.env*` files into the
 * Worker at build time (`next-env.mjs`), so `apps/web/.env.local`'s
 * `INTERNAL_API_URL=http://127.0.0.1:8000` -- the native-development value --
 * silently became the deployed configuration and blanked every page.
 */
export function isLoopbackHost(hostname: string): boolean {
  const host = hostname.trim().toLowerCase().replace(/^\[|\]$/g, "");
  if (host === "localhost" || host.endsWith(".localhost")) return true;
  if (host === "::1" || host === "0.0.0.0") return true;
  const ipv4 = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (ipv4) {
    const first = Number(ipv4[1]);
    return first === 127 || first === 0;
  }
  return false;
}

export function resolveApiOrigin(
  configured = process.env.INTERNAL_API_URL,
  environment = process.env.NODE_ENV,
): string | null {
  const value = configured?.trim();
  if (!value) return environment === "development" ? DEVELOPMENT_API_ORIGIN : null;
  try {
    const url = new URL(value);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    // Docker Compose reaches the API as `http://api:8000`, so only loopback is
    // refused here -- that keeps the containerised deployment working while a
    // leaked `.env.local` can no longer hijack a deployed Worker.
    if (environment !== "development" && isLoopbackHost(url.hostname)) return null;
    return url.origin;
  } catch {
    return null;
  }
}

export function isSelfReferentialOrigin(apiOrigin: string, requestOrigin: string): boolean {
  return new URL(apiOrigin).origin === new URL(requestOrigin).origin;
}

/**
 * The shared secret the API requires on every route except /health, /ready and
 * /calendar/*. A blank value counts as "not configured" so that an empty
 * INTERNAL_API_SECRET cannot masquerade as a working gate.
 */
export function resolveInternalApiSecret(
  configured = process.env.INTERNAL_API_SECRET,
): string | null {
  const value = configured?.trim();
  return value ? value : null;
}

export function isAccessRejection(response: Response): boolean {
  const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";
  return (response.status === 401 || response.status === 403) && contentType.includes("text/html");
}

/**
 * Statuses Cloudflare returns when there is no origin behind the Tunnel: 530
 * ("origin unreachable"), or 502/504 when the connector itself cannot be
 * reached. These are infrastructure failures, not answers from the API, so the
 * proxy should report them the same way it reports a refused connection --
 * otherwise a dead Tunnel surfaces as a bare `事件 API 返回 530` instead of the
 * documented "事件服务暂时不可达".
 *
 * A JSON body means the API answered (a real 503 during startup, say), so only
 * non-JSON bodies are treated as unreachable. Observed live: killing cloudflared
 * made `/api/v1/events` return 530 straight through to the browser.
 */
const UPSTREAM_UNREACHABLE_STATUSES = new Set([502, 503, 504, 530]);

export function isUpstreamUnreachable(response: Response): boolean {
  if (!UPSTREAM_UNREACHABLE_STATUSES.has(response.status)) return false;
  const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";
  return !contentType.includes("application/json");
}

function apiUnavailable(message: string) {
  return Response.json(
    { error: { code:"api_unavailable", message } },
    { status:503, headers:{ "Cache-Control":"no-store" } },
  );
}

function publicAccessDenied(message: string) {
  return Response.json(
    { error:{ code:"public_read_only", message } },
    { status:403, headers:{ "Cache-Control":"no-store" } },
  );
}

export function isPublicApiAllowed(
  method:string,
  path:string[],
  readOnly = process.env.PUBLIC_READ_ONLY === "1",
):boolean {
  if (!readOnly) return true;
  if (method !== "GET" && method !== "HEAD") return false;
  const route = path.join("/");
  return route === "v1/sources" || /^v1\/events(?:\/[^/]+(?:\/sources)?)?$/.test(route);
}

export function sanitizePublicEvents(payload:unknown):unknown {
  const sanitize = (value:Record<string,unknown>) => ({
    ...value,
    notes:null,
    reminder_enabled:false,
  });
  if (!payload || typeof payload !== "object") return payload;
  if ("items" in payload && Array.isArray((payload as { items:unknown }).items)) {
    const value = payload as Record<string,unknown> & { items:unknown[] };
    const items = value.items
      .filter((item):item is Record<string,unknown> => (
        Boolean(item) && typeof item === "object" && !(item as { is_manual?:boolean }).is_manual
      ))
      .map(sanitize);
    return { ...value, items, total:items.length };
  }
  const value = payload as Record<string,unknown>;
  return value.is_manual ? null : sanitize(value);
}

type PrivateApiBinding = {
  fetch(input: Request): Promise<Response>;
};

type RuntimeEnvironment = CloudflareEnv & {
  CALENDAR_API?:PrivateApiBinding;
  PUBLIC_READ_ONLY?:string;
};

async function runtimeEnvironment(): Promise<RuntimeEnvironment|null> {
  try {
    const { env } = await getCloudflareContext({ async:true });
    return env as RuntimeEnvironment;
  } catch {
    return null;
  }
}

async function proxy(request: NextRequest, context: { params: Promise<{ path:string[] }> }) {
  const { path } = await context.params;
  const runtime = process.env.NODE_ENV === "development" ? null : await runtimeEnvironment();
  const publicReadOnly = runtime
    ? runtime.PUBLIC_READ_ONLY === "1"
    : process.env.PUBLIC_READ_ONLY === "1";
  if (!isPublicApiAllowed(request.method, path, publicReadOnly)) {
    return publicAccessDenied("公开链接仅供查看，不能修改日历或读取私有设置");
  }
  const vpcBinding = runtime?.CALENDAR_API ?? null;
  const apiOrigin = vpcBinding ? VPC_API_ORIGIN : resolveApiOrigin();
  if (!apiOrigin) {
    return apiUnavailable("事件服务尚未连接，请配置 Cloudflare VPC 或 INTERNAL_API_URL");
  }
  if (!vpcBinding && isSelfReferentialOrigin(apiOrigin, request.nextUrl.origin)) {
    return apiUnavailable("事件服务地址不能指向当前 Web Worker，请配置独立 API 地址");
  }
  const target = new URL(`/api/${path.join("/")}`, apiOrigin);
  target.search = request.nextUrl.search;
  const headers = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const accessClientId = process.env.CF_ACCESS_CLIENT_ID?.trim();
  const accessClientSecret = process.env.CF_ACCESS_CLIENT_SECRET?.trim();
  if (accessClientId && accessClientSecret) {
    headers.set("CF-Access-Client-Id", accessClientId);
    headers.set("CF-Access-Client-Secret", accessClientSecret);
  }
  // The API has no user accounts, so a public Tunnel would expose every route.
  // This shared secret is the gate: it is added here, never accepted from the
  // browser (see FORWARDED_REQUEST_HEADERS), so visitors cannot forge it.
  const internalApiSecret = resolveInternalApiSecret();
  if (internalApiSecret) headers.set("X-Internal-Api-Secret", internalApiSecret);
  const body = request.method === "GET" || request.method === "HEAD" ? undefined : await request.arrayBuffer();
  const upstreamRequest = new Request(target, {
    method:request.method,
    headers,
    body,
    redirect:"manual",
  });
  let upstream: Response;
  try {
    upstream = vpcBinding
      ? await vpcBinding.fetch(upstreamRequest)
      : await fetch(upstreamRequest, { cache:"no-store" });
  } catch {
    return apiUnavailable("事件服务暂时不可达，请检查后端或 Cloudflare Tunnel");
  }
  if (isAccessRejection(upstream)) {
    return apiUnavailable("私有事件服务拒绝访问，请检查 Tunnel、VPC Service 或 Access Service Token");
  }
  if (isUpstreamUnreachable(upstream)) {
    return apiUnavailable("事件服务暂时不可达，请检查后端或 Cloudflare Tunnel");
  }
  if (publicReadOnly && path[0] === "v1" && path[1] === "events" && upstream.ok) {
    const payload = sanitizePublicEvents(await upstream.json());
    if (payload === null) return publicAccessDenied("人工事件不通过公开链接展示");
    return Response.json(payload, {
      status:upstream.status,
      headers:{ "Cache-Control":"public, max-age=60" },
    });
  }
  const responseHeaders = new Headers(upstream.headers);
  responseHeaders.delete("content-encoding");
  responseHeaders.delete("content-length");
  return new Response(upstream.body, { status:upstream.status, headers:responseHeaders });
}

export const GET = proxy;
export const POST = proxy;
export const PATCH = proxy;
export const PUT = proxy;
export const DELETE = proxy;
