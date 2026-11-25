import type { ProductionConfig, ProductionState, RenderJob } from "../src/lib/production";

export const productionConfig: ProductionConfig = {
  render_enabled: true, narration_enabled: true, tts_provider: "edge", max_music_upload_bytes: 5 * 1024 * 1024,
  voices: [{ id: "en-US-AriaNeural", language: "en", display_name: "Aria · English" }, { id: "zh-CN-XiaoxiaoNeural", language: "zh-CN", display_name: "晓晓 · 简体中文" }],
};
export const production: ProductionState = {
  project_id: "project-1", revision: 3,
  settings: { aspect_ratio: "16:9", narration_enabled: true, language: "en", voice: "en-US-AriaNeural", speaking_rate: "1", burn_subtitles: true,
    opening_title: "", closing_title: "", title_duration_seconds: 2, music_asset_id: null, watermark_asset_id: null, music_volume: 0.15, source_audio_volume: 0, allow_upscale: false },
  scenes: [{ scene_id: "scene-1", scene_key: "scene_intro", order: 1, duration_seconds: 5, narration: "A new day begins", selected_scene_version_id: "version-2", versions: [
    { scene_version_id: "version-2", version_number: 2, clip_asset_id: "clip-2", preview_url: "/api/v1/assets/clip-2/preview", download_url: "/api/v1/assets/clip-2/download", width: 1920, height: 1080 },
    { scene_version_id: "version-1", version_number: 1, clip_asset_id: "clip-1", preview_url: "/api/v1/assets/clip-1/preview", download_url: "/api/v1/assets/clip-1/download", width: 1280, height: 720 },
  ] }],
  music_assets: [], reference_assets: [], render_jobs: [],
};
export const rendered: RenderJob = {
  id: "render-1", status: "succeeded", stage: "completed", progress_percent: 100,
  error_code: null, warnings: ["source_upscaled"], is_current: false, output_asset_id: "final-1", subtitle_asset_id: "subtitle-1",
  preview_url: "/api/v1/assets/final-1/preview", download_url: "/api/v1/assets/final-1/download", srt_url: "/api/v1/assets/subtitle-1/download", created_at: "2026-11-06T00:00:00Z",
};
