import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../src/lib/api";
import { PlanningProject } from "../src/components/planning-project";
import { project, wan } from "./fixtures";
import { board, input } from "./storyboard-fixtures";

const push = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("../src/components/studio-shell", () => ({ useUser: () => ({ id: "user-1", email: "editor@example.test", role: "editor" }) }));
vi.mock("../src/lib/api", async importOriginal => ({ ...await importOriginal<typeof import("../src/lib/api")>(), api: vi.fn() }));
const mockedApi = vi.mocked(api);
beforeEach(() => {
  mockedApi.mockReset(); push.mockReset();
  mockedApi.mockImplementation(async path => {
    if (path === "/models") return [wan] as never;
    if (path === "/projects") return project as never;
    if (path.endsWith("/inputs") || path.endsWith("/imports/docx")) return input as never;
    if (path.endsWith("/storyboard")) return { ...board, revision: 4 } as never;
    throw new Error(`Unexpected request ${path}`);
  });
});

describe("new planning project", () => {
  it("saves full text before reading the input-bound revision and opening the manual storyboard", async () => {
    render(<PlanningProject/>);
    await screen.findByRole("option", { name: "Wan 2.7" });
    await userEvent.type(screen.getByLabelText("Project name"), "Launch campaign");
    await userEvent.type(screen.getByLabelText(/^Video prompt/), "Show a bright morning in the city.");
    await userEvent.selectOptions(screen.getByLabelText("Narration language"), "zh-CN");
    await userEvent.click(screen.getByRole("button", { name: "Save & open storyboard" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith(`/projects/${project.id}/storyboard`));
    const writes = mockedApi.mock.calls.filter(([, init]) => init?.method);
    expect(writes.map(([path, init]) => [path, init?.method])).toEqual([["/projects", "POST"], [`/projects/${project.id}/inputs`, "POST"], [`/projects/${project.id}/storyboard`, "PUT"]]);
    expect(JSON.parse(writes[1][1]?.body as string)).toMatchObject({ kind: "prompt", text: "Show a bright morning in the city." });
    expect(JSON.parse(writes[2][1]?.body as string)).toMatchObject({ expected_revision: 4, language: "zh-CN", title: "Launch campaign", default_model_id: wan.model_id });
    expect(mockedApi.mock.calls.some(([path]) => path.includes("/director/") || path.includes("/generations"))).toBe(false);
  });
  it("uploads DOCX as multipart data and never sends the raw archive to an AI endpoint", async () => {
    render(<PlanningProject/>);
    await screen.findByRole("option", { name: "Wan 2.7" });
    await userEvent.type(screen.getByLabelText("Project name"), "Imported script");
    const picker = screen.getByLabelText("Or import a DOCX script");
    expect(picker).toHaveAttribute("accept", ".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document");
    const document = new File(["test archive"], "campaign.docx", { type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" });
    await userEvent.upload(picker, document);
    expect(screen.getByLabelText(/^Video prompt/)).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Save & open storyboard" }));
    await waitFor(() => expect(push).toHaveBeenCalled());
    const imported = mockedApi.mock.calls.find(([path]) => path.endsWith("/imports/docx"));
    expect(imported?.[1]?.body).toBeInstanceOf(FormData);
    expect((imported?.[1]?.body as FormData).get("file")).toBe(document);
    expect(mockedApi.mock.calls.some(([path]) => path.endsWith("/inputs"))).toBe(false);
  });
  it("allows reselecting the same DOCX after choosing to use text instead", async () => {
    render(<PlanningProject/>);
    await screen.findByRole("option", { name: "Wan 2.7" });
    const picker = screen.getByLabelText("Or import a DOCX script");
    const document = new File(["test archive"], "campaign.docx", { type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" });
    await userEvent.upload(picker, document);
    expect(screen.getByLabelText(/^Video prompt/)).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Use text instead" }));
    expect(picker).toHaveValue("");
    expect(screen.getByLabelText(/^Video prompt/)).toBeEnabled();
    await userEvent.upload(picker, document);
    expect(screen.getByLabelText(/^Video prompt/)).toBeDisabled();
  });
  it("retries an input failure using the existing saved project", async () => {
    let inputAttempts = 0;
    mockedApi.mockImplementation(async path => {
      if (path === "/models") return [wan] as never;
      if (path === "/projects") return project as never;
      if (path.endsWith("/inputs")) { if (++inputAttempts === 1) throw new Error("Import unavailable"); return input as never; }
      if (path.endsWith("/storyboard")) return board as never;
      throw new Error(`Unexpected request ${path}`);
    });
    render(<PlanningProject/>);
    await screen.findByRole("option", { name: "Wan 2.7" });
    await userEvent.type(screen.getByLabelText("Project name"), "Keep my project");
    await userEvent.type(screen.getByLabelText(/^Video prompt/), "A city skyline");
    await userEvent.click(screen.getByRole("button", { name: "Save & open storyboard" }));
    await screen.findByText("Import unavailable");
    await userEvent.click(screen.getByRole("button", { name: "Save & open storyboard" }));
    await waitFor(() => expect(push).toHaveBeenCalled());
    expect(mockedApi.mock.calls.filter(([path]) => path === "/projects")).toHaveLength(1);
    expect(inputAttempts).toBe(2);
  });
});
