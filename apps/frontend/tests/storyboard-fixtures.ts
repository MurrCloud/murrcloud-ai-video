import type { ProjectInput, ReferenceImage, Storyboard, StoryboardEstimate, StoryboardValidation } from "../src/lib/storyboard";
import { project, wan } from "./fixtures";

export const board: Storyboard = {
  project_id: project.id, revision: 3, status: "draft", input_id: "input-1",
  title: "Launch video", target_duration_seconds: 5, language: "en", visual_style: "cinematic",
  target_audience: "Small businesses", default_model_id: wan.model_id,
  scenes: [{ id: "scene-row-1", scene_id: "scene_intro", order: 1, duration_seconds: 5,
    generation_mode: "text_to_video", video_model_id: wan.model_id, resolution: "720p",
    aspect_ratio: "16:9", audio_enabled: true, visual_prompt: "A sunrise over the city",
    negative_prompt: "", narration: "A new day begins", reference_asset_ids: [], transition: "fade", status: "draft" }],
};
export const input: ProjectInput = { id: "input-1", kind: "script", text: "Welcome to a new day.", source_name: "Script.docx", warnings: [], sha256: "a".repeat(64), created_at: "2026-11-06T00:00:00Z" };
export const reference: ReferenceImage = { id: "reference-1", project_id: project.id, original_filename: "city.png", mime_type: "image/png", size_bytes: 200, metadata_json: { width: 640, height: 480, caption: "City inspiration" }, preview_url: "/api/v1/assets/reference-1/preview" };
export const validBoard: StoryboardValidation = { valid: true, revision: 3, total_duration_seconds: 5, target_duration_seconds: 5, tolerance_seconds: 1, issues: [] };
export const planEstimate: StoryboardEstimate = { id: "plan-estimate-1", revision: 3, base_usd: null, total_usd: null, pricing_verified: false, expires_at: "2099-01-01T00:00:00Z", scenes: [{ scene_id: "scene-row-1", scene_key: "scene_intro", estimate_id: "scene-estimate-1", base_usd: null, total_usd: null, pricing_verified: false, source: "unavailable" }] };
