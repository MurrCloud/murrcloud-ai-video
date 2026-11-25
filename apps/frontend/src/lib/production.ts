export type ProductionSettings = {
  aspect_ratio: "16:9" | "9:16" | "1:1";
  narration_enabled: boolean;
  language: "en" | "zh-CN";
  voice: string;
  speaking_rate: string | number;
  burn_subtitles: boolean;
  opening_title: string;
  closing_title: string;
  title_duration_seconds: number;
  music_asset_id: string | null;
  watermark_asset_id: string | null;
  music_volume: string | number;
  source_audio_volume: string | number;
  allow_upscale: boolean;
};
export type ProductionVoice = { id: string; language: "en" | "zh-CN"; display_name: string };
export type ProductionConfig = {
  render_enabled: boolean; narration_enabled: boolean; tts_provider: string;
  voices: ProductionVoice[]; max_music_upload_bytes: number;
};
export type ProductionVersion = {
  scene_version_id: string; version_number: number; clip_asset_id: string;
  preview_url: string | null; download_url: string | null;
  width?: number | null; height?: number | null;
};
export type ProductionScene = {
  scene_id: string; scene_key: string; order: number; duration_seconds: number;
  narration: string; versions: ProductionVersion[]; selected_scene_version_id: string | null;
};
export type RenderJob = {
  id: string; status: string; stage: string; progress_percent: number;
  error_code: string | null; warnings: string[]; is_current: boolean;
  output_asset_id: string | null; subtitle_asset_id: string | null;
  preview_url: string | null; download_url: string | null; srt_url: string | null;
  created_at: string;
};
export type ProductionState = {
  project_id: string; archived?: boolean; revision: number; settings: ProductionSettings;
  scenes: ProductionScene[];
  music_assets: { id: string; original_filename: string | null }[];
  reference_assets: { id: string; preview_url: string | null; original_filename?: string | null }[];
  render_jobs: RenderJob[];
};
export type SceneSelections = Record<string, string>;
export const renderTerminal = new Set(["succeeded", "failed", "cancelled"]);
export const outputDimensions = (aspect: ProductionSettings["aspect_ratio"]) => ({
  "16:9": { width: 1920, height: 1080 }, "9:16": { width: 1080, height: 1920 }, "1:1": { width: 1080, height: 1080 },
})[aspect];
export function initialSelections(state: ProductionState): SceneSelections {
  return Object.fromEntries(state.scenes.filter(scene => scene.selected_scene_version_id).map(scene => [scene.scene_id, scene.selected_scene_version_id!]));
}
export function selectionsComplete(scenes: ProductionScene[], selections: SceneSelections) {
  return scenes.length > 0 && scenes.every(scene => scene.versions.some(version => version.scene_version_id === selections[scene.scene_id]));
}
export function productionDuration(scenes: ProductionScene[], settings: ProductionSettings) {
  return scenes.reduce((total, scene) => total + scene.duration_seconds, 0)
    + (Number(Boolean(settings.opening_title.trim())) + Number(Boolean(settings.closing_title.trim()))) * Number(settings.title_duration_seconds);
}
export function likelyUpscaledScenes(scenes: ProductionScene[], selections: SceneSelections, settings: ProductionSettings) {
  const output = outputDimensions(settings.aspect_ratio);
  return scenes.filter(scene => {
    const version = scene.versions.find(version => version.scene_version_id === selections[scene.scene_id]);
    return version?.width && version?.height && (version.width < output.width || version.height < output.height);
  });
}
export function validProductionSettings(settings: ProductionSettings, config: ProductionConfig | null) {
  const rate = Number(settings.speaking_rate), music = Number(settings.music_volume), source = Number(settings.source_audio_volume);
  return [settings.speaking_rate, settings.music_volume, settings.source_audio_volume].every(value => typeof value !== "string" || value.trim() !== "")
    && ["16:9", "9:16", "1:1"].includes(settings.aspect_ratio)
    && Number.isFinite(rate) && rate >= 0.5 && rate <= 2 && Math.abs(rate * 100 - Math.round(rate * 100)) < 1e-8
    && Number.isFinite(music) && music >= 0 && music <= 1
    && Number.isFinite(source) && source >= 0 && source <= 1
    && Number.isInteger(Number(settings.title_duration_seconds)) && Number(settings.title_duration_seconds) >= 1 && Number(settings.title_duration_seconds) <= 5
    && settings.opening_title.length <= 200 && settings.closing_title.length <= 200
    && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(settings.opening_title + settings.closing_title)
    && (!settings.burn_subtitles || settings.narration_enabled)
    && (!settings.narration_enabled || Boolean(config?.voices.some(voice => voice.id === settings.voice && voice.language === settings.language)));
}
export function validateMusic(file: File, maxBytes: number) {
  if (file.size === 0) return "Choose a nonempty music file.";
  if (file.size > maxBytes) return `Choose a music file no larger than ${Math.floor(maxBytes / 1024 / 1024)} MiB.`;
  if (!/\.(mp3|wav)$/i.test(file.name)) return "Choose an MP3 or WAV file.";
  return null;
}
export function privateAssetLink(value: string | null | undefined) {
  return value && /^\/api\/v1\/assets\/[A-Za-z0-9-]+\/(?:preview|download)$/.test(value) ? value : null;
}
export function productionIssue(code: string) {
  const issues: Record<string, string> = {
    revision_conflict: "The storyboard or production settings changed. Reload the saved project before rendering.",
    rendering_disabled: "Rendering is disabled. An operator must enable the isolated render worker.",
    render_disabled: "Rendering is disabled. An operator must enable the isolated render worker.",
    narration_disabled: "Narration is disabled. Turn narration off or ask an operator to enable the speech adapter.",
    scene_clip_not_ready: "Every active scene needs a completed, matching clip version.",
    clips_not_ready: "Every active scene needs a completed, matching clip version.",
    upscaling_requires_approval: "The output resolution exceeds a source clip. Explicitly allow upscaling or select a larger source.",
    upscale_not_approved: "The output resolution exceeds a source clip. Explicitly allow upscaling or select a larger source.",
    narration_exceeds_scene_duration: "Narration is longer than its scene. Edit the narration, speaking rate or scene duration before rendering.",
    host_admission_refused: "Processing is paused while host resources are unavailable. The worker will check again safely.",
    speech_boundaries_missing: "Speech timing was unavailable. No subtitle timing was invented; retry only after reviewing the speech service.",
  };
  return issues[code] ?? code.replaceAll("_", " ");
}
