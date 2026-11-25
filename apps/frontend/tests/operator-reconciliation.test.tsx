import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../src/lib/api";
import { OperatorReconciliation } from "../src/components/operator-reconciliation";
import type { ReconciliationJob, UnconfirmedCost } from "../src/lib/workspace";
const mock = vi.hoisted(() => ({ role: "admin" }));
vi.mock("../src/components/studio-shell", () => ({ useUser: () => ({ id: "admin-1", role: mock.role, email: "admin@example.test" }) }));
vi.mock("../src/lib/api", async original => ({ ...await original<typeof import("../src/lib/api")>(), api: vi.fn() }));
const mockedApi = vi.mocked(api);
const job: ReconciliationJob = { id: "job-1", kind: "generation", project_id: "project-1", provider: "fal", model_id: "wan", status: "reconciliation_required", updated_at: "2026-11-07T01:00:00Z", reserved_usd: "2", provider_job_id: null };
const cost: UnconfirmedCost = { id: "cost-1", project_id: "project-1", provider: "fal", model_id: "wan", generation_job_id: "job-1", director_job_id: null, rendering_job_id: null, status: "unavailable", amount_usd: null, provider_charge_id: null, updated_at: "2026-11-07T01:00:00Z" };
function install(overrides: (path: string, init?: RequestInit) => unknown = () => undefined) { mockedApi.mockImplementation(async (path, init) => {
  const override = overrides(path, init); if (override !== undefined) return await override as never;
  if (init?.method === "POST") return {} as never;
  if (path.startsWith("/operations/reconciliations?")) { const director = new URLSearchParams(path.split("?")[1]).get("kind") === "director"; return { items: [director ? { ...job, id: "director-1", kind: "director", provider: "openai", model_id: "director-model" } : job], total: 1, limit: 25, offset: 0 } as never; }
  if (path.startsWith("/operations/costs?")) return { items: [cost], total: 1, limit: 25, offset: 0 } as never;
  throw new Error(`Unexpected request: ${path}`);
}); }
beforeEach(() => { mock.role = "admin"; mockedApi.mockReset(); });
async function evidence(label = /verified this exact job and outcome/) { await userEvent.type(screen.getByLabelText(/^Provider statement or support reference/), "support:verified-1"); await userEvent.click(screen.getByLabelText(label)); }

describe("explicit operator reconciliation", () => {
  it("requires verified evidence and a canonical existing UUID before resuming polling", async () => {
    install(); const changed = vi.fn(); render(<OperatorReconciliation refresh={0} onChange={changed}/>);
    await userEvent.click(await screen.findByRole("button", { name: "Review outcome" }));
    await userEvent.selectOptions(screen.getByLabelText("Verified provider outcome"), "attach_provider_id");
    const submit = screen.getByRole("button", { name: "Attach ID & resume polling" }); expect(submit).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/^Existing provider request UUID/), "12345678-1234-1234-1234-123456789abc");
    await evidence(); await userEvent.click(submit); await screen.findByText(/No new paid generation was submitted/);
    const [path, request] = mockedApi.mock.calls.find(([, init]) => init?.method === "POST")!;
    expect(path).toBe("/operations/reconciliations/generation/job-1");
    expect(JSON.parse(request!.body as string)).toEqual({ reference: "support:verified-1", acknowledge_verified: true, expected_updated_at: job.updated_at, outcome: "attach_provider_id", provider_job_id: "12345678-1234-1234-1234-123456789abc", idempotency_key: expect.any(String) });
    expect(changed).toHaveBeenCalledOnce();
    expect(mockedApi.mock.calls.some(([path]) => path.includes("/generations") || path.includes("/director/jobs") || path.includes("/renders"))).toBe(false);
  });
  it("offers only verified failure for director jobs and retains cost uncertainty", async () => {
    install(); render(<OperatorReconciliation refresh={0} onChange={vi.fn()}/>);
    await userEvent.selectOptions(await screen.findByLabelText("Uncertain job type"), "director");
    await screen.findByText("director-model"); await userEvent.click(screen.getByRole("button", { name: "Review outcome" }));
    expect(screen.queryByRole("option", { name: "Attach an existing request ID" })).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Verified provider outcome"), "verified_failed"); await evidence();
    await userEvent.click(screen.getByRole("button", { name: "Record verified failure" })); await screen.findByText(/Reservations and unknown charges remain unchanged/);
    const [path, request] = mockedApi.mock.calls.find(([, init]) => init?.method === "POST")!;
    expect(path).toBe("/operations/reconciliations/director/director-1"); expect(JSON.parse(request!.body as string)).not.toHaveProperty("provider_job_id");
  });
  it("preserves a mutation key after an ambiguous reconciliation response", async () => {
    let attempts = 0; install((_, init) => { if (init?.method === "POST" && ++attempts === 1) throw new ApiError(0, "Connection lost"); });
    render(<OperatorReconciliation refresh={0} onChange={vi.fn()}/>); await userEvent.click(await screen.findByRole("button", { name: "Review outcome" }));
    await userEvent.selectOptions(screen.getByLabelText("Verified provider outcome"), "verified_failed"); await evidence();
    await userEvent.click(screen.getByRole("button", { name: "Record verified failure" })); await screen.findByText("Connection lost");
    await userEvent.click(screen.getByRole("button", { name: "Record verified failure" })); await screen.findByText(/Verified provider failure recorded/);
    const requests = mockedApi.mock.calls.filter(([, init]) => init?.method === "POST").map(([, init]) => JSON.parse(init!.body as string));
    expect(requests).toHaveLength(2); expect(requests[0]).toEqual(requests[1]);
  });
  it("requires an explicitly entered final amount and acknowledges a verified zero charge", async () => {
    install(); const changed = vi.fn(); render(<OperatorReconciliation refresh={0} onChange={changed}/>);
    await userEvent.click(await screen.findByRole("button", { name: "Review charge" }));
    const review = screen.getByRole("region", { name: "Charge confirmation" }); const submit = within(review).getByRole("button", { name: "Record verified charge" });
    expect(screen.getByLabelText(/^Verified final charge/)).toHaveValue(""); expect(submit).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/^Verified final charge/), "0"); await userEvent.type(screen.getByLabelText("Provider charge ID (optional)"), "invoice:zero-1");
    await evidence(/verified this exact final charge/); await userEvent.click(submit); await screen.findByText(/Verified provider charge recorded/);
    const [path, request] = mockedApi.mock.calls.find(([, init]) => init?.method === "POST")!;
    expect(path).toBe("/operations/costs/cost-1/confirm"); expect(JSON.parse(request!.body as string)).toMatchObject({ expected_updated_at: cost.updated_at, amount_usd: "0", provider_charge_id: "invoice:zero-1", acknowledge_verified: true }); expect(changed).toHaveBeenCalledOnce();
  });
  it("preserves evidence on a stale record and requires a fresh review", async () => {
    install((_, init) => { if (init?.method === "POST") throw new ApiError(409, "revision_conflict"); });
    render(<OperatorReconciliation refresh={0} onChange={vi.fn()}/>); await userEvent.click(await screen.findByRole("button", { name: "Review charge" }));
    await userEvent.type(screen.getByLabelText(/^Verified final charge/), "1.250000"); await evidence(/verified this exact final charge/);
    await userEvent.click(screen.getByRole("button", { name: "Record verified charge" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The saved record changed. Reload it before applying another change.");
    expect(screen.getByLabelText(/^Verified final charge/)).toHaveValue("1.250000");
    expect(screen.getByLabelText(/^Provider statement or support reference/)).toHaveValue("support:verified-1");
    expect(screen.getByRole("checkbox", { name: /verified this exact final charge/ })).toBeChecked();
    expect(screen.getByRole("button", { name: "Record verified charge" })).toBeDisabled();
    expect(screen.getByText(/Close this form and refresh status/)).toBeInTheDocument();
  });
  it("clears evidence acknowledgement after any amount change", async () => {
    install(); render(<OperatorReconciliation refresh={0} onChange={vi.fn()}/>); await userEvent.click(await screen.findByRole("button", { name: "Review charge" }));
    await userEvent.type(screen.getByLabelText(/^Verified final charge/), "1"); await evidence(/verified this exact final charge/);
    expect(screen.getByRole("button", { name: "Record verified charge" })).toBeEnabled();
    await userEvent.type(screen.getByLabelText(/^Verified final charge/), ".5"); expect(screen.getByRole("checkbox", { name: /verified this exact final charge/ })).not.toBeChecked(); expect(screen.getByRole("button", { name: "Record verified charge" })).toBeDisabled();
  });
  it("synchronously blocks repeated submissions before the first response settles", async () => {
    let complete: (value: object) => void = () => {}; const pending = new Promise(resolve => { complete = resolve; });
    install((_, init) => init?.method === "POST" ? pending : undefined);
    render(<OperatorReconciliation refresh={0} onChange={vi.fn()}/>); await userEvent.click(await screen.findByRole("button", { name: "Review charge" }));
    await userEvent.type(screen.getByLabelText(/^Verified final charge/), "1"); await evidence(/verified this exact final charge/);
    const form = screen.getByRole("region", { name: "Charge confirmation" }).querySelector("form")!;
    act(() => { fireEvent.submit(form); fireEvent.submit(form); });
    expect(mockedApi.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Recording…" })).toBeDisabled();
    await act(async () => { complete({}); }); await screen.findByText(/Verified provider charge recorded/);
  });
  it("does not load or expose operator records for a non-administrator", async () => {
    mock.role = "editor"; install(); render(<OperatorReconciliation refresh={0} onChange={vi.fn()}/>);
    await Promise.resolve(); expect(mockedApi).not.toHaveBeenCalled(); expect(screen.queryByRole("heading")).not.toBeInTheDocument();
  });
});
