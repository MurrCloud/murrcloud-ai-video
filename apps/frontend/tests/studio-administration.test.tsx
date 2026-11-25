import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../src/lib/api";
import { Dashboard } from "../src/components/dashboard";
import { OperationsPage } from "../src/components/operations-page";
import { ProjectDetails } from "../src/components/project-details";
import { SettingsPage } from "../src/components/settings-page";
import { UsagePage } from "../src/components/usage-page";
import { dashboard, libraryProject, operations, settings, usage } from "./workspace-fixtures";
const mock = vi.hoisted(() => ({ role: "editor", replace: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: mock.replace }) }));
vi.mock("../src/components/studio-shell", () => ({ useUser: () => ({ id: "user-1", role: mock.role, email: "editor@example.test" }) }));
vi.mock("../src/lib/api", async original => ({ ...await original<typeof import("../src/lib/api")>(), api: vi.fn() }));
const mockedApi = vi.mocked(api);
beforeEach(() => { mockedApi.mockReset(); mock.replace.mockReset(); mock.role = "editor"; });

describe("honest cumulative studio reporting", () => {
  it("shows server aggregates independently from a limited recent-project list", async () => {
    mockedApi.mockResolvedValue(dashboard as never); render(<Dashboard/>);
    const summary = await screen.findByRole("region", { name: "Project summary" });
    expect(within(summary).getByText("145")).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { name: "Launch film" })).toHaveLength(1);
    expect(screen.getByText(/1 generation outcome needs reconciliation/)).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(mockedApi).toHaveBeenCalledWith("/dashboard");
    expect(mockedApi.mock.calls.some(([path]) => path.startsWith("/projects?"))).toBe(false);
  });
  it("distinguishes missing charges from an explicitly confirmed zero and changes grouping", async () => {
    mockedApi.mockImplementation(async path => {
      if (!path.startsWith("/reports/usage?")) throw new Error(`Unexpected request: ${path}`);
      if (new URLSearchParams(path.split("?")[1]).get("group_by") === "provider") return { ...usage, scope: "all", totals: { ...usage.totals, confirmed_usd: "0.0000", total_actual_usd: "0.0000", known_cost_count: 1 }, items: [{ ...usage.items[0], label: "fal", project_id: null, confirmed_usd: "0.0000" }] } as never;
      return usage as never;
    });
    render(<UsagePage/>); await screen.findByText("Launch film");
    expect(screen.getAllByText("Unavailable")).toHaveLength(3);
    expect(screen.getByText(/Includes archived projects/)).toBeInTheDocument();
    expect(screen.getByText(/confirmed subtotal may not represent the final cost/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Group costs by"), "provider");
    await screen.findByText("fal");
    expect(screen.queryByText("Unavailable")).not.toBeInTheDocument();
    expect(screen.getAllByText("$0.00")).toHaveLength(3);
    expect(screen.getByText(/All studio projects \(administrator\)/)).toBeInTheDocument();
    const query = new URLSearchParams(mockedApi.mock.calls.at(-1)![0].split("?")[1]);
    expect(Object.fromEntries(query)).toEqual({ group_by: "provider", limit: "50", offset: "0" });
  });
  it("preserves negative budget and authorization balances after a verified charge overrun", async () => {
    mockedApi.mockResolvedValue({ ...usage, totals: { ...usage.totals, exposure_usd: "12", remaining_budget_usd: "-2", remaining_approved_usd: "-8" } } as never);
    render(<UsagePage/>); await screen.findByText(/Spending exposure exceeds/);
    expect(screen.getByText("Configured budgets").nextElementSibling).toHaveTextContent("$10.00");
    expect(screen.getByText("Approved spending caps").nextElementSibling).toHaveTextContent("$4.00");
    expect(screen.getByText("Spending exposure").nextElementSibling).toHaveTextContent("$12.00");
    expect(screen.getByText("Budget remaining").nextElementSibling).toHaveTextContent("-$2.00");
    expect(screen.getByText("Authorization remaining").nextElementSibling).toHaveTextContent("-$8.00");
    expect(screen.getByText(/Totals across projects do not authorize spending/)).toBeInTheDocument();
  });
});

describe("project-specific exposure reporting", () => {
  function installProjectReport(failed = false) { mockedApi.mockImplementation(async path => {
    if (path === "/projects/project-1") return libraryProject as never;
    if (path === "/generation-config") return { paid_generation_enabled: false, provider_configured: false } as never;
    if (path.startsWith("/reports/usage?")) { if (failed) throw new ApiError(503, "report unavailable"); return { ...usage, totals: { ...usage.totals, exposure_usd: "12", remaining_budget_usd: "-2", remaining_approved_usd: "-8" } } as never; }
    return [] as never;
  }); }
  it("uses the authorized project report instead of subtracting reservations locally", async () => {
    installProjectReport(); render(<ProjectDetails id="project-1"/>); await screen.findByRole("heading", { name: "Launch film" });
    expect(screen.getByText("Spending exposure").nextElementSibling).toHaveTextContent("$12.00");
    expect(screen.getByText("Budget remaining").nextElementSibling).toHaveTextContent("-$2.00");
    expect(screen.getByText("Authorization remaining").nextElementSibling).toHaveTextContent("-$8.00");
    expect(screen.getByText(/Negative remaining balances show the deficit/)).toBeInTheDocument();
    const query = new URLSearchParams(mockedApi.mock.calls.find(([path]) => path.startsWith("/reports/usage?"))![0].split("?")[1]);
    expect(query.get("project_id")).toBe("project-1");
  });
  it("marks report failure unavailable without a budget-minus-reservation fallback", async () => {
    installProjectReport(true); render(<ProjectDetails id="project-1"/>); await screen.findByText(/Current spending report could not be loaded/);
    expect(screen.getByText("Spending exposure").nextElementSibling).toHaveTextContent("Unavailable");
    expect(screen.getByText("Budget remaining").nextElementSibling).toHaveTextContent("Unavailable");
    expect(screen.getByText("Authorization remaining").nextElementSibling).toHaveTextContent("Unavailable");
    expect(screen.queryByText("$8.00")).not.toBeInTheDocument();
  });
});

describe("account security settings", () => {
  it("requires a matching new password and redirects after all sessions are revoked", async () => {
    mockedApi.mockImplementation(async (path, init) => {
      if (path === "/settings") return settings as never;
      if (path === "/account/password" && init?.method === "POST") return undefined as never;
      throw new Error(`Unexpected request: ${path}`);
    });
    render(<SettingsPage/>); await screen.findByText("editor@example.test");
    const submit = screen.getByRole("button", { name: "Update password & sign out" });
    expect(submit).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Current password"), "current-password");
    await userEvent.type(screen.getByLabelText(/^New password/), "different-new-password");
    await userEvent.type(screen.getByLabelText("Confirm new password"), "does-not-match");
    expect(submit).toBeDisabled();
    await userEvent.clear(screen.getByLabelText("Confirm new password"));
    await userEvent.type(screen.getByLabelText("Confirm new password"), "different-new-password");
    await userEvent.click(submit);
    await waitFor(() => expect(mock.replace).toHaveBeenCalledWith("/login"));
    const [, request] = mockedApi.mock.calls.find(([path]) => path === "/account/password")!;
    expect(JSON.parse(request!.body as string)).toEqual({ current_password: "current-password", new_password: "different-new-password", confirm_password: "different-new-password" });
    expect(screen.getByLabelText("Current password")).toHaveValue("");
    expect(screen.getByLabelText(/^New password/)).toHaveValue("");
    expect(screen.getByLabelText("Confirm new password")).toHaveValue("");
  });
  it("only displays credential presence and read-only feature gates", async () => {
    mockedApi.mockResolvedValue({ ...settings, providers: { ...settings.providers, fal_configured: true } } as never);
    render(<SettingsPage/>); await screen.findByText("Present; live access not verified here");
    expect(screen.getAllByText("Disabled")).toHaveLength(5);
    expect(screen.queryByRole("button", { name: /enable/i })).not.toBeInTheDocument();
    expect(screen.queryAllByRole("textbox")).toHaveLength(0);
    expect(mockedApi.mock.calls).toHaveLength(1);
  });
});

describe("administrator-only monitoring", () => {
  it("does not request studio-wide monitoring for a non-administrator", async () => {
    render(<OperationsPage/>); expect(screen.getByText(/Administrator access is required/)).toBeInTheDocument();
    await Promise.resolve(); expect(mockedApi).not.toHaveBeenCalled();
  });
  it("shows fail-closed admission, unknown storage, and sanitized audit metadata", async () => {
    mock.role = "admin";
    mockedApi.mockImplementation(async path => {
      if (path === "/operations") return { ...operations, storage: { ...operations.storage, available: false, used_bytes: null, free_bytes: null } } as never;
      if (path === "/operations/audit?limit=50") return { items: [{ id: "audit-1", event_type: "charge_confirmed", project_id: "project-1", entity_type: "generation_job", entity_id: "job-1", created_at: "2026-11-07T01:00:00Z" }], limit: 50, offset: 0, total: 1 } as never;
      throw new Error(`Unexpected request: ${path}`);
    });
    render(<OperationsPage/>); await screen.findByText("New work is blocked");
    expect(screen.getByText(/Storage health is unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/An admission signal does not prove production capacity/)).toBeInTheDocument();
    expect(screen.getByText(/not proof that a worker or provider is reachable/)).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText("charge confirmed")).toBeInTheDocument();
    expect(screen.getByText(/raw prompt\/provider bodies are excluded/)).toBeInTheDocument();
    expect(mockedApi.mock.calls.every(([, init]) => !init?.method)).toBe(true);
  });
});
