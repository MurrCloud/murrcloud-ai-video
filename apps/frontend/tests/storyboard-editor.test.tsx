import { useState } from "react";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { StoryboardEditor } from "../src/components/storyboard-editor";
import { board, reference } from "./storyboard-fixtures";
import { imageModel, kling, wan } from "./fixtures";

function Editor() {
  const [value, setValue] = useState({ ...board, scenes: [{ ...board.scenes[0], duration_seconds: 2, aspect_ratio: "4:3" }] });
  return <StoryboardEditor board={value} models={[wan, kling, imageModel]} references={[reference]} onChange={setValue}/>;
}

describe("storyboard editor", () => {
  it("updates scene capabilities and forced native audio when the model changes", async () => {
    render(<Editor/>);
    expect(screen.getByLabelText("Native audio for scene 1")).toBeChecked();
    expect(screen.getByLabelText("Native audio for scene 1")).toBeDisabled();
    await userEvent.selectOptions(screen.getByLabelText("Video model for scene 1"), kling.model_id);
    expect(screen.getByLabelText("Resolution for scene 1")).toHaveValue("1080p");
    expect(screen.getByLabelText("Duration for scene 1")).toHaveValue("3");
    expect(screen.getByLabelText("Aspect ratio for scene 1")).toHaveValue("16:9");
    expect(screen.getByLabelText("Native audio for scene 1")).toBeEnabled();
    expect(screen.queryByRole("option", { name: /Wan image to video/ })).not.toBeInTheDocument();
  });
  it("keeps scene content through add, reorder and removal", async () => {
    render(<Editor/>);
    await userEvent.click(screen.getByRole("button", { name: "Add scene" }));
    await userEvent.type(screen.getByLabelText("Visual prompt for scene 2"), "Closing skyline");
    await userEvent.type(screen.getByLabelText("Narration for scene 2"), "See you tomorrow");
    await userEvent.click(screen.getByRole("button", { name: "Move scene 2 up" }));
    expect(screen.getByLabelText("Visual prompt for scene 1")).toHaveValue("Closing skyline");
    expect(screen.getByLabelText("Narration for scene 1")).toHaveValue("See you tomorrow");
    await userEvent.click(screen.getByRole("button", { name: "Remove scene 2" }));
    expect(screen.queryByLabelText("Visual prompt for scene 2")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Visual prompt for scene 1")).toHaveValue("Closing skyline");
  });
  it("attaches and replaces private references while clearly describing planning-only use", async () => {
    render(<Editor/>);
    await userEvent.click(screen.getByText("Negative prompt and planning references"));
    const scene = screen.getByRole("article", { name: "Scene 1" });
    await userEvent.click(within(scene).getByRole("checkbox", { name: "City inspiration" }));
    expect(within(scene).getByRole("img", { name: "City inspiration" })).toHaveAttribute("src", "/api/v1/assets/reference-1/preview");
    expect(within(scene).getByText(/Image pixels are not sent/)).toBeInTheDocument();
    await userEvent.click(within(scene).getByRole("checkbox", { name: "City inspiration" }));
    expect(within(scene).queryByRole("img")).not.toBeInTheDocument();
  });
});
