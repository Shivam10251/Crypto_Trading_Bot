import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, fetchSystemStatus } from "../lib/api";

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubFetch(implementation: typeof fetch) {
  vi.stubGlobal("fetch", vi.fn(implementation));
}

describe("fetchSystemStatus", () => {
  it("requests the versioned endpoint", async () => {
    const spy = vi.fn(async () =>
      new Response(JSON.stringify({ profile: "development" }), { status: 200 }),
    );
    vi.stubGlobal("fetch", spy);

    await fetchSystemStatus();

    expect(spy).toHaveBeenCalledWith("/api/v1/system-status");
  });

  it("returns the parsed payload", async () => {
    stubFetch(async () =>
      new Response(JSON.stringify({ profile: "paper", version: "0.1.0" }), { status: 200 }),
    );

    await expect(fetchSystemStatus()).resolves.toMatchObject({
      profile: "paper",
      version: "0.1.0",
    });
  });

  it("raises ApiError with the status code on a failed response", async () => {
    stubFetch(async () => new Response("boom", { status: 503 }));

    const error = await fetchSystemStatus().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(503);
  });

  it("raises ApiError when the backend is unreachable", async () => {
    stubFetch(async () => {
      throw new TypeError("Failed to fetch");
    });

    await expect(fetchSystemStatus()).rejects.toThrow(/cannot reach backend/);
  });
});
