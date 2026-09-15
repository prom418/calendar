import { getCloudflareContext } from "@opennextjs/cloudflare";

export async function GET() {
  // Mirror src/app/api/[...path]/route.ts: development must ignore the
  // Cloudflare runtime. `getCloudflareContext()` resolves even under `next dev`
  // and hands back wrangler.jsonc's vars, so the deployed
  // `PUBLIC_READ_ONLY: "1"` otherwise leaks into local development. The UI would
  // then hide every write control while the proxy still accepted writes, and the
  // E2E write tests (manual drawer, settings, sync) fail on both viewports.
  let readOnly = process.env.PUBLIC_READ_ONLY === "1";
  if (process.env.NODE_ENV !== "development") {
    try {
      const { env } = await getCloudflareContext({ async:true });
      readOnly = (env as CloudflareEnv & { PUBLIC_READ_ONLY?:string }).PUBLIC_READ_ONLY === "1";
    } catch {
      // No Cloudflare runtime context (a plain Node deployment, for instance).
    }
  }
  return Response.json(
    { readOnly },
    { headers:{ "Cache-Control":"no-store" } },
  );
}
