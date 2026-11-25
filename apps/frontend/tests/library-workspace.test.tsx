import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../src/lib/api";
import { LibraryPage } from "../src/components/library-page";
import { TemplatesPage } from "../src/components/templates-page";
import { libraryProject, template } from "./workspace-fixtures";
const role = vi.hoisted(() => ({ value: "editor" }));
vi.mock("../src/components/studio-shell", () => ({ useUser: () => ({ id: "user-1", role: role.value, email: "editor@example.test" }) }));
vi.mock("../src/lib/api", async original => ({ ...await original<typeof import("../src/lib/api")>(), api: vi.fn() }));
const mockedApi = vi.mocked(api);
beforeEach(() => { mockedApi.mockReset(); role.value = "editor"; });
function library(overrides: (path: string, init?: RequestInit) => unknown = () => undefined) { mockedApi.mockImplementation(async (path, init) => { const override = overrides(path, init); if (override !== undefined) return await override as never; if (path.startsWith("/library/projects?")) return { items: [libraryProject], total: 1, offset: 0, limit: 24 } as never; if (path.endsWith("/duplicate")) return { ...libraryProject, id: "copy-1", name: "Launch film copy" } as never; if (path.endsWith("/templates")) return template as never; if (path.endsWith("/metadata") || path.endsWith("/archive")) return libraryProject as never; throw new Error(`Unexpected request: ${path}`); }); }
function templates(overrides: (path: string, init?: RequestInit) => unknown = () => undefined) { mockedApi.mockImplementation(async (path, init) => { const override = overrides(path, init); if (override !== undefined) return await override as never; if (path.startsWith("/templates?")) return { items: [template], total: 1, offset: 0, limit: 24 } as never; if (path.endsWith("/apply")) return { ...libraryProject, id: "draft-1" } as never; if (path === "/templates/template-1") return { ...template, revision: 3 } as never; throw new Error(`Unexpected request: ${path}`); }); }

describe("owned library workflows", () => {
  it("shows private video links and sends literal search/status/archive filters", async () => {
    library(); render(<LibraryPage/>); await screen.findByRole("heading", { name: "Launch film" });
    expect(screen.getByLabelText("Video preview for Launch film")).toHaveAttribute("src", "/api/v1/assets/final-1/preview");
    expect(screen.getByRole("link", { name: "MP4" })).toHaveAttribute("href", "/api/v1/assets/final-1/download");
    await userEvent.type(screen.getByLabelText("Search projects"), "中文 %_"); await userEvent.selectOptions(screen.getByLabelText("Project status"), "completed"); await userEvent.click(screen.getByLabelText("Show archived projects")); await userEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => { const call = mockedApi.mock.calls.filter(([path]) => path.startsWith("/library/projects?")).at(-1)![0]; const query = new URLSearchParams(call.split("?")[1]); expect(query.get("q")).toBe("中文 %_"); expect(query.get("archived")).toBe("true"); expect(query.get("status")).toBe("completed"); });
  });
  it("reuses a duplicate key after ambiguous response and never calls generation endpoints", async () => {
    let attempts = 0; library((path) => { if (path.endsWith("/duplicate") && ++attempts === 1) throw new ApiError(0, "Connection lost"); });
    render(<LibraryPage/>); await userEvent.click(await screen.findByRole("button", { name: "Duplicate" }));
    expect(screen.getByLabelText("Project name")).toHaveFocus(); await userEvent.click(screen.getByRole("button", { name: "Create draft copy" })); await screen.findByText("Connection lost");
    await userEvent.click(screen.getByRole("button", { name: "Create draft copy" })); await screen.findByText(/Spending approvals and media were not copied/);
    const requests = mockedApi.mock.calls.filter(([path]) => path.endsWith("/duplicate")).map(([, init]) => JSON.parse(init!.body as string));
    expect(requests).toHaveLength(2); expect(requests[0].idempotency_key).toBe(requests[1].idempotency_key);
    expect(Object.keys(requests[0]).sort()).toEqual(["idempotency_key", "name"]);
    expect(mockedApi.mock.calls.some(([path]) => path.includes("generations") || path.includes("/renders") || path.includes("director"))).toBe(false);
  });
  it("binds metadata to its saved timestamp and preserves a stale edit", async () => {
    library((path) => { if (path.endsWith("/metadata")) throw new ApiError(409, "project_metadata_conflict"); }); render(<LibraryPage/>);
    await userEvent.click(await screen.findByRole("button", { name: "Edit details" })); const form = screen.getByRole("region", { name: "Project action" });
    await userEvent.clear(within(form).getByLabelText("Project name")); await userEvent.type(within(form).getByLabelText("Project name"), "My revised title"); await userEvent.click(within(form).getByRole("button", { name: "Save" }));
    await screen.findByText(/The project changed/); expect(within(form).getByLabelText("Project name")).toHaveValue("My revised title"); expect(within(form).getByRole("button", { name: "Save" })).toBeDisabled();
    const request = mockedApi.mock.calls.find(([path]) => path.endsWith("/metadata"))!; expect(JSON.parse(request[1]!.body as string).expected_updated_at).toBe(libraryProject.updated_at);
  });
  it("requires explicit administrator acknowledgement before sharing template text", async () => {
    role.value = "admin"; library(); render(<LibraryPage/>); await userEvent.click(await screen.findByRole("button", { name: "Save template" }));
    await userEvent.click(screen.getByLabelText("Share this template with studio users")); const form = screen.getByRole("region", { name: "Project action" }); expect(within(form).getByRole("button", { name: "Save" })).toBeDisabled();
    await userEvent.click(screen.getByLabelText(/I reviewed the prompts/)); await userEvent.click(within(form).getByRole("button", { name: "Save" }));
    await screen.findByText(/Template saved/); const request = mockedApi.mock.calls.find(([path]) => path.endsWith("/templates"))!; expect(JSON.parse(request[1]!.body as string)).toMatchObject({ shared: true, acknowledge_shared_content: true });
  });
  it("archives the saved project timestamp without changing or deleting its media", async () => {
    library(); render(<LibraryPage/>); await userEvent.click(await screen.findByRole("button", { name: "Archive" }));
    await screen.findByText(/History and media are retained/);
    const mutations = mockedApi.mock.calls.filter(([, init]) => init?.method);
    expect(mutations).toHaveLength(1);
    expect(mutations[0][0]).toBe("/projects/project-1/archive");
    expect(JSON.parse(mutations[0][1]!.body as string)).toEqual({ expected_updated_at: libraryProject.updated_at, archived: true });
  });
  it("keeps viewer controls read-only", async () => { role.value = "viewer"; library(); render(<LibraryPage/>); await screen.findByRole("heading", { name: "Launch film" }); expect(screen.queryByRole("button", { name: "Duplicate" })).not.toBeInTheDocument(); expect(screen.queryByRole("button", { name: "Archive" })).not.toBeInTheDocument(); expect(screen.getByRole("link", { name: "MP4" })).toBeInTheDocument(); });
});

describe("template reuse", () => {
  it("creates an unapproved draft with an explicit budget and stable application key", async () => { templates(); render(<TemplatesPage/>); await userEvent.click(await screen.findByRole("button", { name: "Use template" })); await userEvent.clear(screen.getByLabelText(/^Maximum project budget/)); await userEvent.type(screen.getByLabelText(/^Maximum project budget/), "3.5000"); await userEvent.click(screen.getByRole("button", { name: "Create draft project" })); const link = await screen.findByRole("link", { name: "Open new storyboard" }); expect(link).toHaveAttribute("href", "/projects/draft-1/storyboard"); const request = mockedApi.mock.calls.find(([path]) => path.endsWith("/apply"))!; expect(JSON.parse(request[1]!.body as string)).toMatchObject({ name: "Product launch project", budget_usd: "3.5000", idempotency_key: expect.any(String) }); });
  it("lets an owner make a shared template private before editing", async () => { templates(path => path.startsWith("/templates?") ? { items: [{ ...template, shared: true }], total: 1, limit: 24, offset: 0 } : undefined); render(<TemplatesPage/>); await userEvent.click(await screen.findByRole("button", { name: "Edit details" })); const save = screen.getByRole("button", { name: "Save template" }); expect(save).toBeDisabled(); await userEvent.click(screen.getByLabelText(/Make this shared template private/)); await userEvent.click(save); await screen.findByText("Template details saved."); const request = mockedApi.mock.calls.find(([path]) => path === "/templates/template-1")!; expect(JSON.parse(request[1]!.body as string)).toMatchObject({ expected_revision: 2, shared: false }); });
  it("requires fresh administrator acknowledgement before archiving a shared template", async () => {
    role.value = "admin"; templates(path => path.startsWith("/templates?") ? { items: [{ ...template, shared: true }], total: 1, limit: 24, offset: 0 } : undefined);
    render(<TemplatesPage/>); await userEvent.click(await screen.findByRole("button", { name: "Archive" }));
    const submit = screen.getByRole("button", { name: "Archive reviewed template" }); expect(submit).toBeDisabled();
    expect(mockedApi.mock.calls.every(([, init]) => !init?.method)).toBe(true);
    await userEvent.click(screen.getByLabelText(/acknowledge this shared content change/)); await userEvent.click(submit);
    await screen.findByText(/Template archived/);
    const request = mockedApi.mock.calls.find(([path]) => path === "/templates/template-1")!;
    expect(JSON.parse(request[1]!.body as string)).toEqual({ expected_revision: 2, archived: true, acknowledge_shared_content: true });
  });
  it("does not apply corrupted, archived or viewer-only templates", async () => { templates(path => path.startsWith("/templates?") ? { items: [{ ...template, plan_invalid: true, target_duration_seconds: null, language: null }], total: 1, limit: 24, offset: 0 } : undefined); render(<TemplatesPage/>); await screen.findByText(/operator review/); expect(screen.queryByRole("button", { name: "Use template" })).not.toBeInTheDocument(); });
});
