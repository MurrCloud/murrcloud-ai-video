import { settingsForModel } from "./model-settings";
import type { ClipSettings, VideoModel } from "./types";

export type StoryboardScene = {
  id?: string;
  scene_id: string;
  order: number;
  duration_seconds: number;
  generation_mode: string;
  video_model_id: string;
  resolution: string;
  aspect_ratio: string;
  audio_enabled: boolean;
  visual_prompt: string;
  negative_prompt: string;
  narration: string;
  reference_asset_ids: string[];
  transition: "cut" | "fade" | "dissolve";
  status: "draft";
};

export type Storyboard = {
  project_id: string;
  revision: number;
  status: string;
  input_id: string | null;
  title: string;
  target_duration_seconds: number;
  language: "en" | "zh-CN";
  visual_style: string;
  target_audience: string;
  default_model_id: string | null;
  scenes: StoryboardScene[];
};

export type ProjectInput = {
  id: string; kind: "prompt" | "script" | "docx"; text: string;
  source_name: string | null; sha256: string; warnings: string[]; created_at: string;
};

export type ReferenceImage = {
  id: string; project_id: string; original_filename: string | null;
  mime_type: string; size_bytes: number; preview_url: string;
  metadata_json: { width: number; height: number; caption?: string; [key: string]: unknown };
};

export type StoryboardValidation = {
  valid: boolean; revision: number; total_duration_seconds: number;
  target_duration_seconds: number; tolerance_seconds: number;
  issues: { scene_id: string | null; code: string }[];
};

export type StoryboardEstimate = {
  id: string; revision: number; base_usd: string | null; total_usd: string | null;
  pricing_verified: boolean; expires_at: string;
  scenes: { scene_id: string; scene_key: string; estimate_id: string;
    base_usd: string | null; total_usd: string | null; pricing_verified: boolean; source: string }[];
};

export type DirectorConfig = {
  director_enabled: boolean; provider_configured: boolean;
  requires_operator_approval: boolean; model?: string | null;
};

export type DirectorJob = {
  id: string; status: string; input_id: string; board_revision: number;
  result_revision: number | null; reserved_cost_usd: string; actual_cost_usd: string | null;
  usage: Record<string, number>; error_code: string | null; dispatch_state: string;
};

export const directorTerminal = new Set(["succeeded", "failed", "cancelled", "stale", "reconciliation_required"]);
export const sceneSettings = (scene: StoryboardScene): ClipSettings => ({
  model_id: scene.video_model_id, duration_seconds: scene.duration_seconds,
  resolution: scene.resolution, aspect_ratio: scene.aspect_ratio,
  mode: scene.generation_mode, audio_enabled: scene.audio_enabled,
});

export function changeSceneModel(scene: StoryboardScene, model: VideoModel): StoryboardScene {
  const settings = settingsForModel(model, sceneSettings(scene));
  return { ...scene, video_model_id: model.model_id, duration_seconds: settings.duration_seconds,
    generation_mode: settings.mode, resolution: settings.resolution,
    aspect_ratio: settings.aspect_ratio, audio_enabled: settings.audio_enabled, status: "draft" };
}

export function newScene(model: VideoModel, order: number): StoryboardScene {
  const settings = settingsForModel(model);
  return { scene_id: `scene_${crypto.randomUUID().replaceAll("-", "")}`,
    order, duration_seconds: settings.duration_seconds, generation_mode: settings.mode,
    video_model_id: settings.model_id, resolution: settings.resolution,
    aspect_ratio: settings.aspect_ratio, audio_enabled: settings.audio_enabled,
    visual_prompt: "", negative_prompt: "", narration: "", reference_asset_ids: [],
    transition: "fade", status: "draft" };
}

export function reorderScenes(scenes: StoryboardScene[], from: number, to: number): StoryboardScene[] {
  if (from < 0 || to < 0 || from >= scenes.length || to >= scenes.length) return scenes;
  const reordered = [...scenes];
  const [scene] = reordered.splice(from, 1);
  reordered.splice(to, 0, scene);
  return reordered.map((item, index) => ({ ...item, order: index + 1 }));
}

export function boardPayload(board: Storyboard) {
  return { expected_revision: board.revision, title: board.title,
    target_duration_seconds: board.target_duration_seconds, language: board.language,
    visual_style: board.visual_style, target_audience: board.target_audience,
    default_model_id: board.default_model_id,
    scenes: board.scenes.map((scene, index) => ({ ...scene, order: index + 1, status: "draft" as const })) };
}

export function validateUpload(file: File, kind: "docx" | "image"): string | null {
  if (file.size > 5 * 1024 * 1024) return "Choose a file no larger than 5 MiB.";
  if (!file.size) return "The selected file is empty.";
  if (kind === "docx" && !file.name.toLowerCase().endsWith(".docx")) return "Choose a DOCX document.";
  if (kind === "image" && !["image/jpeg", "image/png", "image/webp"].includes(file.type)) return "Choose a PNG, JPEG or WebP image.";
  return null;
}

export function storyboardIssue(code: string): string {
  const messages: Record<string, string> = {
    no_scenes: "Add at least one scene.", empty_prompt: "Add a visual prompt.",
    duration_mismatch: "Scene durations must match the target duration within tolerance.",
    unsupported_duration: "Choose a supported clip duration.", model_disabled: "Choose an available model.",
    unsupported_model: "Choose a verified model.", reference_asset_not_found: "Choose a reference image owned by this project.",
    unresolved_paid_jobs: "A generation needs completion or operator reconciliation before this plan can change.",
    revision_conflict: "A newer storyboard was saved. Reload it before saving your changes.",
  };
  return messages[code] ?? code.replaceAll("_", " ");
}
