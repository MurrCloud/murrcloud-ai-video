import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../src/lib/api";
import { StoryboardWorkspace } from "../src/components/storyboard-workspace";
import { config, estimate, project, wan } from "./fixtures";
import { board, input, planEstimate, reference, validBoard } from "./storyboard-fixtures";

vi.mock("../src/components/studio-shell", () => ({ useUser: () => ({ id: "user-1", email: "editor@example.test", role: "editor" }) }));
vi.mock("../src/lib/api", async importOriginal => ({ ...await importOriginal<typeof import("../src/lib/api")>(), api: vi.fn() }));
const mockedApi = vi.mocked(api);
const base = `/projects/${project.id}`;

function installApi(overrides: (path: string, init?: RequestInit) => unknown = () => undefined) {
  mockedApi.mockImplementation(async (path, init) => {
    const override = overrides(path, init);
    if (override !== undefined) return await override as never;
    if (path === `${base}/storyboard`) return JSON.parse(JSON.stringify(board)) as never;
    if (path === base) return project as never;
    if (path === "/models") return [wan] as never;
    if (path === `${base}/inputs`) return [input] as never;
    if (path === `${base}/assets/reference-images` || path === `${base}/director/jobs`) return [] as never;
    if (path === "/director-config") return { director_enabled: false, provider_configured: false, requires_operator_approval: true } as never;
    if (path === "/generation-config") return config as never;
    if (path === `${base}/storyboard/validate`) return validBoard as never;
    if (path === `${base}/storyboard/estimate`) return planEstimate as never;
    if (path === `${base}/generations/estimate`) return estimate as never;
    throw new Error(`Unexpected request: ${path}`);
  });
}
beforeEach(() => { mockedApi.mockReset(); });

describe("saved storyboard workflow", () => {
  it("loads a completed AI result even when the first job snapshot is already terminal", async () => {
    let boardReads = 0;
    installApi(path => {
      if (path === `${base}/storyboard`) {
        return ++boardReads === 1 ? board : { ...board, revision: 4, title: "AI planned launch", scenes: [{ ...board.scenes[0], visual_prompt: "A polished AI production plan" }] };
      }
      if (path === `${base}/director/jobs`) return [{ id: "director-1", status: "succeeded", input_id: input.id, board_revision: 3, result_revision: 4, reserved_cost_usd: "1", actual_cost_usd: null, usage: {}, error_code: null, dispatch_state: "queued" }];
    });
    render(<StoryboardWorkspace projectId={project.id}/>);
    await waitFor(() => expect(screen.getByLabelText("Visual prompt for scene 1")).toHaveValue("A polished AI production plan"));
    expect(screen.getByText("Saved revision 4")).toBeInTheDocument();
    expect(boardReads).toBe(2);
    expect(mockedApi.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });
  it("keeps AI planning disabled without operator configuration and blocks validation of local edits", async () => {
    installApi(); render(<StoryboardWorkspace projectId={project.id}/>);
    await screen.findByRole("heading", { name: "Your story, scene by scene" });
    const director = screen.getByRole("button", { name: "Generate AI storyboard" });
    expect(director).toBeDisabled();
    expect(screen.getByText("AI planning is disabled.")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Visual prompt for scene 1"), " in soft light");
    expect(screen.getByRole("button", { name: "Validate saved storyboard" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Estimate generation for scene 1" })).toBeDisabled();
    await userEvent.click(director);
    expect(mockedApi.mock.calls.some(([path, init]) => path.endsWith("/director/jobs") && init?.method === "POST")).toBe(false);
    expect(screen.getByText("Unsaved changes.")).toBeInTheDocument();
  });
  it("preserves local scene edits on a revision conflict and explicitly reloads the saved plan", async () => {
    let reload = false;
    installApi((path, init) => {
      if (path === `${base}/storyboard` && init?.method === "PUT") throw new ApiError(409, "revision_conflict");
      if (path === `${base}/storyboard` && reload) return { ...board, revision: 4 };
    });
    render(<StoryboardWorkspace projectId={project.id}/>);
    const prompt = await screen.findByLabelText("Visual prompt for scene 1");
    await userEvent.type(prompt, " with orange clouds");
    await userEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await screen.findByText("The saved plan changed.");
    expect(prompt).toHaveValue("A sunrise over the city with orange clouds");
    const savedCall = mockedApi.mock.calls.find(([path, init]) => path === `${base}/storyboard` && init?.method === "PUT");
    expect(JSON.parse(savedCall?.[1]?.body as string)).toMatchObject({ expected_revision: 3 });
    expect(screen.getByRole("button", { name: "Save draft" })).toBeDisabled();
    reload = true;
    await userEvent.click(screen.getByRole("button", { name: "Discard local changes & reload" }));
    await waitFor(() => expect(screen.getByLabelText("Visual prompt for scene 1")).toHaveValue(board.scenes[0].visual_prompt));
    expect(screen.getByText("Saved revision 4")).toBeInTheDocument();
  });
  it("shows every timing and model validation issue instead of exposing approval", async () => {
    installApi(path => path === `${base}/storyboard/validate` ? { ...validBoard, valid: false, total_duration_seconds: 2, issues: [{ scene_id: "scene_intro", code: "empty_prompt" }, { scene_id: null, code: "duration_mismatch" }] } : undefined);
    render(<StoryboardWorkspace projectId={project.id}/>);
    await userEvent.click(await screen.findByRole("button", { name: "Validate saved storyboard" }));
    await screen.findByText("This plan needs attention");
    expect(screen.getByText("scene_intro: Add a visual prompt.")).toBeInTheDocument();
    expect(screen.getByText("Scene durations must match the target duration within tolerance.")).toBeInTheDocument();
    expect(screen.getByText("2s of 5s target · tolerance 1s")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Estimate all scenes" })).toBeDisabled();
    expect(screen.queryByRole("region", { name: "Generation approval" })).not.toBeInTheDocument();
  });
  it("binds the whole-plan quote to a saved revision and requires explicit unknown-price approval", async () => {
    installApi(path => path === `${base}/storyboard/approve` ? { jobs: [] } : undefined);
    render(<StoryboardWorkspace projectId={project.id}/>);
    await userEvent.click(await screen.findByRole("button", { name: "Validate saved storyboard" }));
    await screen.findByText("Plan validated");
    await userEvent.click(screen.getByRole("button", { name: "Estimate all scenes" }));
    const approve = await screen.findByRole("button", { name: "Approve & generate all scenes" });
    expect(approve).toBeDisabled();
    expect(screen.getByText("Quote bound to revision 3")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox", { name: /I approve this total project/ }));
    expect(approve).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox", { name: /I acknowledge unavailable/ }));
    await userEvent.click(approve);
    await waitFor(() => expect(mockedApi.mock.calls.some(([path]) => path === `${base}/storyboard/approve`)).toBe(true));
    const request = mockedApi.mock.calls.find(([path]) => path === `${base}/storyboard/approve`);
    expect(JSON.parse(request?.[1]?.body as string)).toMatchObject({ expected_revision: 3, estimate_id: planEstimate.id, approved_cap_usd: "10", approve_unverified_price: true });
  });
  it("invalidates an existing quote when the script changes without calling paid endpoints", async () => {
    installApi(); render(<StoryboardWorkspace projectId={project.id}/>);
    await userEvent.click(await screen.findByRole("button", { name: "Validate saved storyboard" }));
    await screen.findByText("Plan validated");
    await userEvent.click(screen.getByRole("button", { name: "Estimate all scenes" }));
    await screen.findByRole("button", { name: "Approve & generate all scenes" });
    await userEvent.type(screen.getByLabelText("Script or prompt"), " Updated instructions.");
    expect(screen.getByRole("button", { name: "Approve & generate all scenes" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Estimate all scenes" })).toBeDisabled();
    expect(mockedApi.mock.calls.some(([path]) => path === `${base}/storyboard/approve` || path === `${base}/generations`)).toBe(false);
  });
  it("warns on browser unload only while scene or input changes remain unsaved", async () => {
    installApi(); render(<StoryboardWorkspace projectId={project.id}/>);
    const prompt = await screen.findByLabelText("Visual prompt for scene 1");
    const clean = new Event("beforeunload", { cancelable: true }); window.dispatchEvent(clean);
    expect(clean.defaultPrevented).toBe(false);
    await userEvent.type(prompt, " with soft light");
    const edited = new Event("beforeunload", { cancelable: true }); window.dispatchEvent(edited);
    expect(edited.defaultPrevented).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Discard local changes & reload" }));
    await waitFor(() => expect(screen.getByLabelText("Visual prompt for scene 1")).toHaveValue(board.scenes[0].visual_prompt));
    const reloaded = new Event("beforeunload", { cancelable: true }); window.dispatchEvent(reloaded);
    expect(reloaded.defaultPrevented).toBe(false);
    await userEvent.type(screen.getByLabelText("Script or prompt"), " Revised.");
    const inputEdit = new Event("beforeunload", { cancelable: true }); window.dispatchEvent(inputEdit);
    expect(inputEdit.defaultPrevented).toBe(true);
  });
  it("allows selecting the same reference file again after a successful upload", async () => {
    let uploads = 0;
    installApi((path, init) => {
      if (path === `${base}/assets/reference-images`) {
        if (init?.method === "POST") { uploads++; return reference; }
        return uploads ? [reference] : [];
      }
    });
    render(<StoryboardWorkspace projectId={project.id}/>);
    const picker = await screen.findByLabelText("Reference image");
    const image = new File(["test pixels"], "city.png", { type: "image/png" });
    await userEvent.upload(picker, image);
    await userEvent.click(screen.getByRole("button", { name: "Upload private reference" }));
    await screen.findByText("Private reference added. Attach it to a scene and save the draft.");
    expect(picker).toHaveValue("");
    await userEvent.upload(picker, image);
    expect(screen.getByRole("button", { name: "Upload private reference" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Upload private reference" }));
    await waitFor(() => expect(uploads).toBe(2));
  });
});
