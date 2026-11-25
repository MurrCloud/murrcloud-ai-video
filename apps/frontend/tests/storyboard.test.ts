import { describe, expect, it } from "vitest";
import { boardPayload, changeSceneModel, directorTerminal, newScene, reorderScenes, validateUpload } from "../src/lib/storyboard";
import { kling, wan } from "./fixtures";
import { board } from "./storyboard-fixtures";

describe("storyboard planning contracts", () => {
  it("switches models without keeping unsupported settings or mutating saved identity", () => {
    const scene = { ...board.scenes[0], duration_seconds: 2, aspect_ratio: "4:3", resolution: "720p" };
    const changed = changeSceneModel(scene, kling);
    expect(changed).toMatchObject({ id: scene.id, scene_id: scene.scene_id, duration_seconds: 3, resolution: "1080p", aspect_ratio: "16:9", video_model_id: kling.model_id });
    expect(scene.resolution).toBe("720p");
    expect(changeSceneModel({ ...changed, audio_enabled: false }, wan).audio_enabled).toBe(true);
  });
  it("reorders stable scene IDs and canonicalizes order without mutating versions", () => {
    const second = { ...board.scenes[0], id: "scene-row-2", scene_id: "scene_close", order: 2 };
    const original = [board.scenes[0], second];
    expect(reorderScenes(original, 0, 1).map(scene => [scene.id, scene.order])).toEqual([["scene-row-2", 1], ["scene-row-1", 2]]);
    expect(original[0].scene_id).toBe("scene_intro");
    expect(reorderScenes(original, -1, 0)).toBe(original);
  });
  it("creates unique blank scene keys without generating media", () => {
    const first = newScene(wan, 1), second = newScene(wan, 2);
    expect(first.scene_id).toMatch(/^[A-Za-z0-9_.-]{1,64}$/);
    expect(first.scene_id).not.toBe(second.scene_id);
    expect(first).toMatchObject({ visual_prompt: "", narration: "", reference_asset_ids: [], status: "draft", audio_enabled: true });
    expect(first.id).toBeUndefined();
  });
  it("sends optimistic revision and editor fields without read-only metadata", () => {
    const payload = boardPayload({ ...board, scenes: [{ ...board.scenes[0], order: 8 }] });
    expect(payload.expected_revision).toBe(3);
    expect(payload.scenes[0].order).toBe(1);
    expect(payload).not.toHaveProperty("project_id");
    expect(payload).not.toHaveProperty("input_id");
    expect(payload).not.toHaveProperty("revision");
  });
  it("rejects oversized, empty or unsupported upload selections before submission", () => {
    expect(validateUpload(new File(["script"], "script.DOCX"), "docx")).toBeNull();
    expect(validateUpload(new File(["script"], "script.doc"), "docx")).toBe("Choose a DOCX document.");
    expect(validateUpload(new File([], "script.docx"), "docx")).toContain("empty");
    const tooBig = new File(["x"], "script.docx"); Object.defineProperty(tooBig, "size", { value: 5 * 1024 * 1024 + 1 });
    expect(validateUpload(tooBig, "docx")).toContain("5 MiB");
    expect(validateUpload(new File(["<svg/>"], "image.svg", { type: "image/svg+xml" }), "image")).toContain("PNG, JPEG or WebP");
    expect(validateUpload(new File(["pixels"], "image.png", { type: "image/png" }), "image")).toBeNull();
  });
  it("allows a new planning attempt after a stale terminal result", () => {
    expect(directorTerminal.has("stale")).toBe(true);
    expect(directorTerminal.has("submitting")).toBe(false);
  });
});
