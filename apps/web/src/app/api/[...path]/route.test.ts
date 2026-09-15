import { describe, expect, it } from "vitest";

import {
  isAccessRejection,
  isPublicApiAllowed,
  isSelfReferentialOrigin,
  resolveApiOrigin,
  resolveInternalApiSecret,
  sanitizePublicEvents,
} from "./route";

describe("API proxy origin", () => {
  it("does not point a production Worker at its own loopback interface", () => {
    expect(resolveApiOrigin(undefined, "production")).toBeNull();
  });

  it("keeps the local API fallback for development", () => {
    expect(resolveApiOrigin(undefined, "development")).toBe("http://127.0.0.1:8000");
  });

  it("normalizes an explicitly configured origin", () => {
    expect(resolveApiOrigin("https://api.example.com/path", "production")).toBe("https://api.example.com");
  });

  it("rejects malformed and unsupported origins", () => {
    expect(resolveApiOrigin("not-a-url", "production")).toBeNull();
    expect(resolveApiOrigin("file:///tmp/api", "production")).toBeNull();
  });

  it("rejects a production API origin that points back at the same Worker", () => {
    expect(isSelfReferentialOrigin(
      "https://calendar.example.com",
      "https://calendar.example.com/api/v1/events",
    )).toBe(true);
    expect(isSelfReferentialOrigin(
      "https://api.example.com",
      "https://calendar.example.com",
    )).toBe(false);
  });

  it("recognizes Cloudflare Access HTML rejections without masking JSON API errors", () => {
    expect(isAccessRejection(new Response("forbidden", {
      status:403,
      headers:{ "content-type":"text/html; charset=UTF-8" },
    }))).toBe(true);
    expect(isAccessRejection(new Response(JSON.stringify({ error:"forbidden" }), {
      status:403,
      headers:{ "content-type":"application/json" },
    }))).toBe(false);
  });

  it("allows only public event and source reads in public mode", () => {
    expect(isPublicApiAllowed("GET", ["v1", "events"], true)).toBe(true);
    expect(isPublicApiAllowed("GET", ["v1", "events", "event-id"], true)).toBe(true);
    expect(isPublicApiAllowed("GET", ["v1", "events", "event-id", "sources"], true)).toBe(true);
    expect(isPublicApiAllowed("GET", ["v1", "sources"], true)).toBe(true);
    expect(isPublicApiAllowed("POST", ["v1", "sync"], true)).toBe(false);
    expect(isPublicApiAllowed("GET", ["v1", "settings"], true)).toBe(false);
    expect(isPublicApiAllowed("GET", ["v1", "changes"], true)).toBe(false);
  });

  it("removes manual events and private fields from public responses", () => {
    expect(sanitizePublicEvents({
      items:[
        { id:"official", is_manual:false, notes:"private", reminder_enabled:true },
        { id:"manual", is_manual:true, notes:"personal" },
      ],
      total:2,
    })).toEqual({
      items:[{ id:"official", is_manual:false, notes:null, reminder_enabled:false }],
      total:1,
    });
  });
});

describe("internal API secret", () => {
  it("treats unset and blank values as no gate rather than an open one", () => {
    expect(resolveInternalApiSecret(undefined)).toBeNull();
    expect(resolveInternalApiSecret("")).toBeNull();
    expect(resolveInternalApiSecret("   ")).toBeNull();
  });

  it("trims a configured secret so the Worker and API agree byte for byte", () => {
    expect(resolveInternalApiSecret("  shared-secret-value  ")).toBe("shared-secret-value");
  });
});
