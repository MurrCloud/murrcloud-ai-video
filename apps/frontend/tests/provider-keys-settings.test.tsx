import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../src/lib/api";
import type { ProviderKeySettings } from "../src/lib/workspace";
import { SettingsPage } from "../src/components/settings-page";
import { settings } from "./workspace-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }) }));
vi.mock("../src/lib/api", async original => ({ ...await original<typeof import("../src/lib/api")>(), api: vi.fn() }));
const mockedApi = vi.mocked(api);
const route = "/settings/provider-keys";
const secret = "synthetic-provider-value-".repeat(2);
const password = "current-offline-admin-password";
const keyStatus: ProviderKeySettings = { storage_available: true, paid_generation_enabled: false, director_enabled: false, providers: [
  { provider: "openai", stored: false, revision: 0, updated_at: null, runtime_configured: false, usage_enabled: false },
  { provider: "fal", stored: false, revision: 0, updated_at: null, runtime_configured: false, usage_enabled: false },
] };
const saved = (): ProviderKeySettings => ({ ...keyStatus, providers: keyStatus.providers.map(provider => provider.provider === "openai" ? { ...provider, stored: true, revision: 1, updated_at: "2026-11-08T00:00:00Z" } : provider) });

beforeEach(() => { mockedApi.mockReset(); });
function install(status = keyStatus) {
  mockedApi.mockImplementation(async path => {
    if (path === "/settings") return { ...settings, user: { ...settings.user, role: "admin" } } as never;
    if (path === route) return status as never;
    throw new Error("Unexpected request");
  });
}
async function openForm(name = "OpenAI") {
  const form = await screen.findByRole("form", { name: `${name} API key` });
  return { form, key: within(form).getByLabelText(new RegExp(`${name.replace(".", "\\.")} API key`)), password: within(form).getByLabelText(`Administrator password for ${name}`) };
}

describe("administrator provider keys", () => {
  it.each(["editor", "viewer"])("never renders or requests key management for a %s", async role => {
    mockedApi.mockResolvedValue({ ...settings, user: { ...settings.user, role } } as never);
    render(<SettingsPage/>); await screen.findByText(settings.user.email);
    expect(screen.queryByRole("heading", { name: "Provider API keys" })).not.toBeInTheDocument();
    expect(mockedApi.mock.calls.map(([path]) => path)).toEqual(["/settings"]);
  });

  it("requires masked new credentials and explicit saving without any enable or test control", async () => {
    install(); render(<SettingsPage/>); const fields = await openForm();
    expect(fields.key).toHaveAttribute("type", "password");
    expect(fields.key).toHaveAttribute("autocomplete", "off");
    expect(fields.password).toHaveAttribute("type", "password");
    const save = within(fields.form).getByRole("button", { name: "Save OpenAI key" });
    expect(save).toBeDisabled();
    await userEvent.type(fields.key, secret); expect(save).toBeDisabled();
    await userEvent.type(fields.password, password); expect(save).toBeEnabled();
    expect(screen.getByText(/Saving a key does not enable paid generation or make a test request/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /enable|test|reveal|show.*key/i })).not.toBeInTheDocument();
    expect(mockedApi.mock.calls.every(([, options]) => !options?.method)).toBe(true);
  });

  it("saves with the expected revision and clears credentials after success", async () => {
    install(); const initial = mockedApi.getMockImplementation()!;
    mockedApi.mockImplementation(async (path, options) => path === `${route}/openai` && options?.method === "PUT" ? saved() as never : initial(path, options));
    render(<SettingsPage/>); const fields = await openForm();
    await userEvent.type(fields.key, secret); await userEvent.type(fields.password, password);
    await userEvent.click(within(fields.form).getByRole("button", { name: "Save OpenAI key" }));
    await screen.findByText(/OpenAI key saved securely/);
    const requests = mockedApi.mock.calls.filter(([, options]) => options?.method);
    expect(requests).toHaveLength(1);
    expect(requests[0][0]).toBe(`${route}/openai`);
    expect(JSON.parse(requests[0][1]!.body as string)).toEqual({ api_key: secret, current_password: password, expected_revision: 0 });
    expect(within(fields.form).getByLabelText(/Replacement OpenAI API key/)).toHaveValue("");
    expect(fields.password).toHaveValue("");
    expect(screen.getByText("Stored securely · awaiting approval")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(secret);
    expect(document.body.textContent).not.toContain(password);
  });

  it("preserves a saved key when its input is blank and removes it only through the explicit action", async () => {
    install(saved()); const initial = mockedApi.getMockImplementation()!;
    mockedApi.mockImplementation(async (path, options) => path === `${route}/openai` && options?.method === "DELETE" ? { ...keyStatus, providers: keyStatus.providers.map(provider => provider.provider === "openai" ? { ...provider, revision: 2 } : provider) } as never : initial(path, options));
    render(<SettingsPage/>); const fields = await openForm();
    await userEvent.type(fields.password, password);
    expect(within(fields.form).getByRole("button", { name: "Replace OpenAI key" })).toBeDisabled();
    await userEvent.click(within(fields.form).getByRole("button", { name: "Remove OpenAI key" }));
    await screen.findByText(/OpenAI key removed/);
    const [, options] = mockedApi.mock.calls.find(([, options]) => options?.method === "DELETE")!;
    expect(JSON.parse(options!.body as string)).toEqual({ current_password: password, expected_revision: 1 });
    expect(fields.password).toHaveValue("");
    expect(screen.getAllByText("No saved key")).toHaveLength(2);
  });

  it("refreshes after a revision conflict and never repeats a mutation automatically", async () => {
    install(); let reads = 0; let writes = 0; const initial = mockedApi.getMockImplementation()!;
    mockedApi.mockImplementation(async (path, options) => {
      if (options?.method === "PUT") {
        if (++writes === 1) throw new ApiError(409, secret);
        return { ...saved(), providers: saved().providers.map(provider => provider.provider === "openai" ? { ...provider, revision: 2 } : provider) } as never;
      }
      if (path === route && ++reads > 1) return saved() as never;
      return initial(path, options);
    });
    render(<SettingsPage/>); const fields = await openForm();
    await userEvent.type(fields.key, secret); await userEvent.type(fields.password, password);
    await userEvent.click(within(fields.form).getByRole("button", { name: "Save OpenAI key" }));
    await screen.findByText(/The saved keys changed/);
    await screen.findByText("Stored securely · awaiting approval");
    expect(within(fields.form).getByLabelText(/Replacement OpenAI API key/)).toHaveValue("");
    expect(fields.password).toHaveValue("");
    expect(mockedApi.mock.calls.filter(([, options]) => options?.method === "PUT")).toHaveLength(1);
    expect(document.body.textContent).not.toContain(secret);
    await userEvent.type(within(fields.form).getByLabelText(/Replacement OpenAI API key/), secret);
    await userEvent.type(fields.password, password);
    await userEvent.click(within(fields.form).getByRole("button", { name: "Replace OpenAI key" }));
    await screen.findByText(/OpenAI key saved securely/);
    const latest = mockedApi.mock.calls.filter(([, options]) => options?.method === "PUT").at(-1)!;
    expect(JSON.parse(latest[1]!.body as string).expected_revision).toBe(1);
  });

  it("disables changes if encrypted storage is unavailable", async () => {
    install({ ...saved(), storage_available: false }); render(<SettingsPage/>); const fields = await openForm();
    expect(fields.key).toBeDisabled(); expect(fields.password).toBeDisabled();
    expect(within(fields.form).getByRole("button", { name: "Remove OpenAI key" })).toBeDisabled();
    expect(screen.getByText(/Contact the Studio operator before adding keys/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh key status" })).toBeEnabled();
  });

  it("clears credentials, hides unsanitized errors and disables stale forms when status refresh fails", async () => {
    install(); let reads = 0; const initial = mockedApi.getMockImplementation()!;
    mockedApi.mockImplementation(async (path, options) => {
      if (options?.method) throw new ApiError(500, secret + password);
      if (path === route && ++reads > 1) throw new ApiError(503, secret);
      return initial(path, options);
    });
    render(<SettingsPage/>); const fields = await openForm();
    await userEvent.type(fields.key, secret); await userEvent.type(fields.password, password);
    await userEvent.click(within(fields.form).getByRole("button", { name: "Save OpenAI key" }));
    await screen.findByText(/Provider key status could not be refreshed/);
    expect(screen.queryByRole("form", { name: "OpenAI API key" })).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain(secret);
    expect(document.body.textContent).not.toContain(password);
    expect(mockedApi.mock.calls.filter(([, options]) => options?.method)).toHaveLength(1);
  });

  it("prevents overlapping key submissions while a change is pending", async () => {
    install(); let release!: (value: never) => void; const initial = mockedApi.getMockImplementation()!;
    mockedApi.mockImplementation(async (path, options) => options?.method ? await new Promise(resolve => { release = resolve; }) : initial(path, options));
    render(<SettingsPage/>); const fields = await openForm();
    await userEvent.type(fields.key, secret); await userEvent.type(fields.password, password);
    await userEvent.dblClick(within(fields.form).getByRole("button", { name: "Save OpenAI key" }));
    expect(mockedApi.mock.calls.filter(([, options]) => options?.method)).toHaveLength(1);
    expect(fields.key).toBeDisabled();
    const other = await openForm("fal.ai"); expect(other.key).toBeDisabled();
    release(saved() as never);
    await waitFor(() => expect(fields.password).toHaveValue(""));
  });
});
