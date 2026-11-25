import type { Estimate, GenerationConfig, Project, VideoModel } from "../src/lib/types";

export const wan: VideoModel = {
  model_id: "fal-ai/wan/v2.7/text-to-video", display_name: "Wan 2.7", endpoint_id: "fal-ai/wan/v2.7/text-to-video", provider: "fal",
  supported_generation_modes: ["text_to_video"], supported_resolutions: ["720p", "1080p"], supported_aspect_ratios: ["16:9", "9:16", "1:1", "4:3"],
  min_duration_seconds: 2, max_duration_seconds: 15, supports_reference_images: false, supports_audio: true, enabled: true, is_default: true,
  price_usd: null, pricing_unit: "second", pricing_details: { public_rates: { "720p": "0.10", "1080p": "0.15" }, audio_control: "always_on", resolution_control: "selectable" },
  documentation_url: "https://fal.ai/models/fal-ai/wan/v2.7/text-to-video/api", verified_at: "2026-11-06T00:00:00Z",
};
export const kling: VideoModel = {
  ...wan, model_id: "fal-ai/kling-video/v3/pro/text-to-video", endpoint_id: "fal-ai/kling-video/v3/pro/text-to-video", display_name: "Kling 3 Pro", is_default: false,
  min_duration_seconds: 3, supported_resolutions: ["1080p"], supported_aspect_ratios: ["16:9", "9:16", "1:1"],
  pricing_details: { public_rates: { audio_off: "0.112", audio_on: "0.168" }, audio_control: "selectable", resolution_control: "fixed" },
};
export const imageModel: VideoModel = { ...wan, model_id: "wan-image", display_name: "Wan image to video", supported_generation_modes: ["image_to_video"], supports_reference_images: true, enabled: false, is_default: false, availability_reason: "Reference uploads arrive Phase 3" };
export const config: GenerationConfig = { paid_generation_enabled: true, provider_configured: true, requires_operator_approval: true };
export const estimate: Estimate = { id: "estimate-1", base_usd: "0.5", total_usd: "0.6", pricing_verified: false, source: "public_model_page", expires_at: "2099-01-01T00:00:00Z", request: {} };
export const project: Project = { id: "project-1", owner_id: "user-1", name: "Test project", prompt: "A sunrise", status: "draft", target_duration_seconds: 5, budget_usd: "10", committed_cost_usd: "0", approved_cap_usd: null };
