import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../src/lib/api";
import { ProductionWorkspace } from "../src/components/production-workspace";
import type { ProductionSettings, ProductionState } from "../src/lib/production";
import { production, productionConfig, rendered } from "./production-fixtures";

const role = vi.hoisted(() => ({ value: "editor" }));
vi.mock("../src/components/studio-shell", () => ({ useUser: () => ({ id: "user-1", email: "editor@example.test", role: role.value }) }));
vi.mock("../src/lib/api", async importOriginal => ({ ...await importOriginal<typeof import("../src/lib/api")>(), api: vi.fn() }));
const mockedApi = vi.mocked(api);
const base = "/projects/project-1";
const clone = (): ProductionState => JSON.parse(JSON.stringify(production));
function installApi(overrides: (path: string, init?: RequestInit) => unknown = () => undefined) {
  mockedApi.mockImplementation(async (path, init) => {
    const value = overrides(path, init);
    if (value !== undefined) return await value as never;
    if (path === "/production-config") return productionConfig as never;
    if (path === `${base}/production`) return clone() as never;
    if (path === `${base}/renders` && init?.method === "POST") return { ...rendered, status: "pending", stage: "queued", progress_percent: 0, output_asset_id: null, preview_url: null, download_url: null, srt_url: null } as never;
    if (path === `${base}/renders`) return [] as never;
    if (path === `${base}/production/settings`) return { ...clone(), revision: 4, settings: (JSON.parse(init!.body as string) as { settings: ProductionSettings }).settings } as never;
    throw new Error(`Unexpected request: ${path}`);
  });
}
beforeEach(() => { mockedApi.mockReset(); role.value = "editor"; });
afterEach(() => { vi.useRealTimers(); });

async function ready() {
  render(<ProductionWorkspace projectId="project-1"/>);
  await screen.findByRole("heading", { name: "Bring your scenes together" });
}

describe("private video production workspace", () => {
  it("keeps rendering disabled while allowing settings preparation", async () => {
    installApi(path => path === "/production-config" ? { ...productionConfig, render_enabled: false } : undefined);
    await ready();
    expect(screen.getByRole("button", { name: "Rendering disabled" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Opening title"), "Welcome");
    expect(screen.getByRole("button", { name: "Save production settings" })).toBeEnabled();
    expect(screen.getByText("Expected duration 7s, including title cards")).toBeInTheDocument();
    expect(mockedApi.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });
  it("submits explicit retained clip versions only after review, without paid generation calls", async () => {
    installApi(); await ready();
    expect(screen.getByRole("button", { name: "Render reviewed video" })).toBeDisabled();
    await userEvent.selectOptions(screen.getByLabelText("Clip version for scene 1"), "version-1");
    expect(screen.getByText(/The selected source is smaller/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox", { name: /I allow source footage/ }));
    await userEvent.click(screen.getByRole("button", { name: "Save production settings" }));
    await screen.findByText(/Production settings saved/);
    expect(screen.getByLabelText("Clip version for scene 1")).toHaveValue("version-1");
    await userEvent.click(screen.getByRole("checkbox", { name: /I reviewed these clip versions/ }));
    await userEvent.click(screen.getByRole("button", { name: "Render reviewed video" }));
    await screen.findByText(/The selected clip snapshot is queued/);
    const request = mockedApi.mock.calls.find(([path, init]) => path === `${base}/renders` && init?.method === "POST");
    expect(JSON.parse(request![1]!.body as string)).toMatchObject({ expected_revision: 4, scene_versions: { "scene-1": "version-1" } });
    expect(JSON.parse(request![1]!.body as string).idempotency_key.length).toBeGreaterThanOrEqual(16);
    expect(mockedApi.mock.calls.some(([path]) => path.includes("/generations") || path.includes("/director"))).toBe(false);
  });
  it("can disable narration and subtitles to render without an enabled speech service", async () => {
    installApi(path => path === "/production-config" ? { ...productionConfig, narration_enabled: false } : undefined);
    await ready();
    await userEvent.click(screen.getByRole("checkbox", { name: /I reviewed these clip versions/ }));
    expect(screen.getByRole("button", { name: "Render reviewed video" })).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox", { name: /Generate narration from/ }));
    expect(screen.getByRole("checkbox", { name: /Burn speech-aligned subtitles/ })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Burn speech-aligned subtitles/ })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Save production settings" }));
    await screen.findByText(/Production settings saved/);
    const save = mockedApi.mock.calls.find(([path]) => path.endsWith("/production/settings"));
    expect(JSON.parse(save![1]!.body as string).settings).toMatchObject({ narration_enabled: false, burn_subtitles: false });
    await userEvent.click(screen.getByRole("checkbox", { name: /I reviewed these clip versions/ }));
    expect(screen.getByRole("button", { name: "Render reviewed video" })).toBeEnabled();
  });
  it("preserves local settings on a revision conflict until an explicit reload", async () => {
    installApi((path, init) => {
      if (path.endsWith("/production/settings") && init?.method === "PUT") throw new ApiError(409, "revision_conflict");
    });
    await ready();
    await userEvent.type(screen.getByLabelText("Opening title"), "Local title");
    await userEvent.click(screen.getByRole("button", { name: "Save production settings" }));
    await screen.findByText("A newer project revision exists.");
    expect(screen.getByLabelText("Opening title")).toHaveValue("Local title");
    expect(screen.getByRole("button", { name: "Save production settings" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Discard local changes & reload" }));
    await waitFor(() => expect(screen.getByLabelText("Opening title")).toHaveValue(""));
  });
  it("retains a submission key after a network failure so retry cannot create a duplicate job", async () => {
    installApi((path, init) => {
      if (path === `${base}/renders` && init?.method === "POST") throw new ApiError(0, "The request could not reach the server.");
    });
    await ready();
    await userEvent.click(screen.getByRole("checkbox", { name: /I reviewed these clip versions/ }));
    await userEvent.click(screen.getByRole("button", { name: "Render reviewed video" }));
    await screen.findByText("The request could not reach the server.");
    await userEvent.click(screen.getByRole("button", { name: "Render reviewed video" }));
    await waitFor(() => expect(mockedApi.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(2));
    const keys = mockedApi.mock.calls.filter(([, init]) => init?.method === "POST").map(([, init]) => JSON.parse(init!.body as string).idempotency_key);
    expect(keys[0]).toBe(keys[1]);
  });
  it("starts an explicitly reviewed new attempt with a fresh key after an uncertain request is confirmed failed", async () => {
    let submissions = 0;
    const failed = { ...rendered, status: "failed", stage: "rendering", is_current: true, error_code: "production_failed", output_asset_id: null, preview_url: null, download_url: null, srt_url: null };
    installApi((path, init) => {
      if (path === `${base}/renders` && init?.method === "POST") {
        if (++submissions === 1) throw new ApiError(0, "The response was lost.");
        return { ...failed, id: "render-2", status: "pending", stage: "narration" };
      }
      if (path === `${base}/renders`) return [failed];
    });
    await ready();
    await userEvent.click(screen.getByRole("checkbox", { name: /I reviewed these clip versions/ }));
    await userEvent.click(screen.getByRole("button", { name: "Render reviewed video" }));
    await screen.findByText("The response was lost.");
    await userEvent.click(screen.getByRole("button", { name: "Refresh progress" }));
    const retry = await screen.findByRole("button", { name: "New render attempt" });
    expect(retry).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox", { name: /I reviewed these clip versions/ }));
    expect(retry).toBeEnabled();
    await userEvent.click(retry);
    await screen.findByText(/The selected clip snapshot is queued/);
    const requests = mockedApi.mock.calls.filter(([path, init]) => path === `${base}/renders` && init?.method === "POST").map(([, init]) => JSON.parse(init!.body as string));
    expect(requests).toHaveLength(2);
    expect(requests[0].idempotency_key).not.toBe(requests[1].idempotency_key);
    expect(requests[1]).toMatchObject({ expected_revision: 3, scene_versions: { "scene-1": "version-2" } });
    expect(screen.getByRole("button", { name: "New render attempt" })).toBeDisabled();
    expect(mockedApi.mock.calls.some(([path]) => path.includes("/generations") || path.includes("/director"))).toBe(false);
  });
  it.each(["historical snapshot", "render gate", "narration gate", "unsaved settings", "active production"])("blocks a new attempt for %s", async reason => {
    const failed = { ...rendered, status: "failed", stage: "rendering", is_current: reason !== "historical snapshot", output_asset_id: null, preview_url: null, download_url: null, srt_url: null };
    const jobs = reason === "active production" ? [failed, { ...failed, id: "render-active", status: "running" }] : [failed];
    installApi(path => {
      if (path === `${base}/production`) return { ...clone(), render_jobs: jobs };
      if (path === "/production-config") return { ...productionConfig, render_enabled: reason !== "render gate", narration_enabled: reason !== "narration gate" };
    });
    await ready();
    await userEvent.click(screen.getByRole("checkbox", { name: /I reviewed these clip versions/ }));
    if (reason === "unsaved settings") await userEvent.type(screen.getByLabelText("Opening title"), "Local edit");
    const retry = screen.getByRole("button", { name: "New render attempt" });
    expect(retry).toBeDisabled();
    await userEvent.click(retry);
    expect(mockedApi.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });
  it("shows private retained MP4/SRT links and marks historical previews", async () => {
    installApi(path => path === `${base}/production` ? { ...clone(), render_jobs: [rendered] } : undefined);
    await ready();
    expect(screen.getByLabelText("Completed video preview")).toHaveAttribute("src", "/api/v1/assets/final-1/preview");
    expect(screen.getByText("HISTORICAL SNAPSHOT")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download final MP4" })).toHaveAttribute("href", "/api/v1/assets/final-1/download");
    expect(screen.getByRole("link", { name: "Download subtitles SRT" })).toHaveAttribute("href", "/api/v1/assets/subtitle-1/download");
    expect(screen.getByText("source upscaled")).toBeInTheDocument();
  });
  it("blocks rendering when a scene has no completed clip", async () => {
    installApi(path => path === `${base}/production` ? { ...clone(), scenes: [{ ...production.scenes[0], versions: [], selected_scene_version_id: null }] } : undefined);
    await ready();
    expect(screen.getByText(/No completed clip is available/)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /I reviewed these clip versions/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Render reviewed video" })).toBeDisabled();
  });
  it("uploads music as multipart and attaches it only to unsaved settings", async () => {
    installApi((path, init) => path.endsWith("/assets/music") && init?.method === "POST" ? { id: "music-1", original_filename: "theme.mp3" } : undefined);
    await ready();
    const file = new File(["synthetic audio"], "theme.mp3", { type: "audio/mpeg" });
    await userEvent.upload(screen.getByLabelText("Upload background music"), file);
    await userEvent.click(screen.getByRole("button", { name: "Upload private music" }));
    await screen.findByText(/Music uploaded privately/);
    const upload = mockedApi.mock.calls.find(([path]) => path.endsWith("/assets/music"));
    expect(upload![1]!.body).toBeInstanceOf(FormData);
    expect((upload![1]!.body as FormData).get("file")).toBe(file);
    expect(screen.getByLabelText("Background music")).toHaveValue("music-1");
    expect(screen.getByRole("button", { name: "Render reviewed video" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save production settings" })).toBeEnabled();
  });
  it("restricts viewer mutation controls while leaving completed downloads accessible", async () => {
    role.value = "viewer";
    installApi(path => path === `${base}/production` ? { ...clone(), render_jobs: [rendered] } : undefined);
    await ready();
    expect(screen.getByLabelText("Opening title")).toBeDisabled();
    expect(screen.getByLabelText("Clip version for scene 1")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Render reviewed video" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Save production settings" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download final MP4" })).toHaveAttribute("href", "/api/v1/assets/final-1/download");
  });
  it("keeps archived project media available while blocking every production mutation", async () => {
    installApi(path => path === `${base}/production` ? { ...clone(), archived: true, render_jobs: [rendered] } : undefined);
    await ready();
    expect(screen.getByText(/Archived projects are read-only/)).toBeInTheDocument();
    expect(screen.getByLabelText("Opening title")).toBeDisabled();
    expect(screen.getByLabelText("Clip version for scene 1")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Save production settings" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Render reviewed video" })).toBeDisabled();
    expect(screen.getByRole("link", { name: "Download final MP4" })).toHaveAttribute("href", "/api/v1/assets/final-1/download");
    expect(mockedApi.mock.calls.every(([, init]) => !init?.method)).toBe(true);
  });
  it("updates progress without overwriting locally edited export settings", async () => {
    vi.useFakeTimers();
    const running = { ...rendered, status: "running", stage: "rendering", progress_percent: 25, output_asset_id: null, preview_url: null, download_url: null, srt_url: null };
    installApi(path => {
      if (path === `${base}/production`) return { ...clone(), render_jobs: [running] };
      if (path === `${base}/renders`) return [{ ...running, progress_percent: 75 }];
    });
    render(<ProductionWorkspace projectId="project-1"/>);
    await act(async () => { await Promise.resolve(); });
    fireEvent.change(screen.getByLabelText("Opening title"), { target: { value: "Keep my local edit" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByLabelText("Opening title")).toHaveValue("Keep my local edit");
    expect(screen.getByRole("progressbar")).toHaveAttribute("value", "75");
    const unload = new Event("beforeunload", { cancelable: true }); window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);
  });
});
