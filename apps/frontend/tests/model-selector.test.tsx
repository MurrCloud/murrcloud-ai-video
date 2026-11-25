import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ModelSelector, modelDocs } from "../src/components/model-selector";
import { settingsForModel } from "../src/lib/model-settings";
import { imageModel, kling, wan } from "./fixtures";

function Selector() {
  const [settings, setSettings] = useState(settingsForModel(wan, { duration_seconds: 2, resolution: "720p", aspect_ratio: "4:3" }));
  return <ModelSelector models={[wan, kling, imageModel]} settings={settings} onChange={setSettings}/>;
}
describe("model selector", () => {
  it("changes the available resolution, duration and aspect options with the model", async () => {
    render(<Selector/>);
    expect(screen.getByLabelText("Resolution")).toHaveValue("720p");
    expect(screen.getByLabelText("Clip duration")).toHaveValue("2");
    expect(screen.getByLabelText("Aspect ratio")).toHaveValue("4:3");
    await userEvent.click(screen.getByRole("radio", { name: "Kling 3 Pro" }));
    expect(screen.getByLabelText("Resolution")).toHaveValue("1080p");
    expect(screen.getByLabelText("Resolution").children).toHaveLength(1);
    expect(screen.getByLabelText("Clip duration")).toHaveValue("3");
    expect(screen.getByLabelText("Aspect ratio")).toHaveValue("16:9");
    expect(screen.queryByRole("option", { name: "4:3" })).not.toBeInTheDocument();
  });
  it("accurately locks always-on audio and disables unavailable reference models", async () => {
    render(<Selector/>);
    const audio = screen.getByRole("checkbox", { name: /Native model audio/ });
    expect(audio).toBeChecked(); expect(audio).toBeDisabled();
    expect(screen.getByRole("radio", { name: "Wan image to video" })).toBeDisabled();
    expect(screen.getByText("Reference uploads arrive Phase 3")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("radio", { name: "Kling 3 Pro" }));
    expect(screen.getByRole("checkbox", { name: /Native model audio/ })).toBeEnabled();
  });
  it("allows only official HTTPS documentation links", () => {
    expect(modelDocs("https://fal.ai/models/example")).toBe("https://fal.ai/models/example");
    expect(modelDocs("javascript:alert(1)")).toBeNull();
    expect(modelDocs("https://fal.ai.attacker.test/")).toBeNull();
    expect(modelDocs(null)).toBeNull();
  });
});
