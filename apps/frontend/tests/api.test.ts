import { describe, expect, it, vi } from "vitest";
import { api, money } from "../src/lib/api";

describe("same-origin API client", () => {
  it("sends HttpOnly-session requests without frontend provider credentials", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    await expect(api("/models")).resolves.toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/models", expect.objectContaining({ credentials: "include", cache: "no-store" }));
    const options = fetchMock.mock.calls[0][1];
    expect(options?.headers).not.toHaveProperty("Authorization");
  });
  it("does not reflect raw HTML or network details in errors", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("<secret>upstream detail</secret>", { status: 502 }));
    await expect(api("/models")).rejects.toThrow("The request could not be completed");
  });
  it("lets the browser provide multipart boundaries for private uploads", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ id: "asset-1" }), { status: 201 }));
    const body = new FormData(); body.append("file", new File(["script"], "script.docx"));
    await api("/projects/project-1/imports/docx", { method: "POST", body });
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/projects/project-1/imports/docx", expect.objectContaining({ body, credentials: "include" }));
    expect(fetchMock.mock.calls[0][1]?.headers).not.toHaveProperty("Content-Type");
  });
  it("preserves verified microcharges and negative remaining balances", () => {
    expect(money("0.000001")).toBe("$0.000001");
    expect(money("-0.000001")).toBe("-$0.000001");
    expect(money("-2")).toBe("-$2.00");
  });
  it("keeps unavailable prices distinct from a zero charge", () => {
    expect(money(null)).toBe("Unavailable"); expect(money(undefined)).toBe("Unavailable");
    expect(money("0")).toBe("$0.00"); expect(money("garbage")).toBe("Unavailable");
  });
});
