import type { ClipSettings, Estimate, GenerationConfig, VideoModel } from "./types";

export const defaultModel = (models: VideoModel[]) => models.find(m => m.enabled && m.is_default) ?? models.find(m => m.enabled);
export function settingsForModel(model: VideoModel, previous?: Partial<ClipSettings>): ClipSettings {
  const keep = (items: string[], value: string | undefined) => value && items.includes(value) ? value : items[0] ?? "";
  const durations = model.supported_duration_seconds ?? model.pricing_details.supported_duration_seconds;
  let duration = Math.min(model.max_duration_seconds, Math.max(model.min_duration_seconds, previous?.duration_seconds ?? 5));
  if (durations?.length && !durations.includes(duration)) duration = durations.reduce((a, b) => Math.abs(b - duration) < Math.abs(a - duration) ? b : a);
  return {
    model_id: model.model_id, duration_seconds: duration,
    resolution: keep(model.supported_resolutions, previous?.resolution),
    aspect_ratio: keep(model.supported_aspect_ratios, previous?.aspect_ratio),
    mode: keep(model.supported_generation_modes, previous?.mode),
    audio_enabled: model.pricing_details.audio_control === "always_on" ? true : model.supports_audio ? previous?.audio_enabled ?? false : false,
  };
}

export function validateSettings(model: VideoModel | undefined, settings: ClipSettings): string | null {
  if (!model?.enabled) return "Select an available model.";
  if (model.model_id !== settings.model_id) return "Model settings need to be refreshed.";
  if (!model.supported_generation_modes.includes(settings.mode)) return "This generation mode is not supported.";
  if (settings.mode !== "text_to_video") return "Reference-image generation will be available with validated uploads.";
  if (!model.supported_resolutions.includes(settings.resolution)) return "Choose a supported resolution.";
  if (!model.supported_aspect_ratios.includes(settings.aspect_ratio)) return "Choose a supported aspect ratio.";
  if (!Number.isInteger(settings.duration_seconds) || settings.duration_seconds < model.min_duration_seconds || settings.duration_seconds > model.max_duration_seconds) return "Choose a supported clip duration.";
  const durations = model.supported_duration_seconds ?? model.pricing_details.supported_duration_seconds;
  if (durations?.length && !durations.includes(settings.duration_seconds)) return "Choose a supported clip duration.";
  if (settings.audio_enabled && !model.supports_audio) return "This model does not support native audio.";
  if (model.pricing_details.audio_control === "always_on" && !settings.audio_enabled) return "This model always generates native audio.";
  return null;
}

export function publicClipPrice(model: VideoModel, settings: ClipSettings): number | null {
  const rates = model.pricing_details.public_rates;
  const raw = rates?.[settings.resolution] ?? rates?.[settings.audio_enabled ? "audio_on" : "audio_off"] ?? (model.pricing_unit === "second" ? model.price_usd : null);
  if (raw == null || !Number.isFinite(Number(raw)) || Number(raw) < 0) return null;
  return Number(raw) * settings.duration_seconds;
}

export function generationBlockReason(config: GenerationConfig | null, estimate: Estimate | null, budgetApproved: boolean, priceApproved: boolean, cap: string, budget: string, committed = "0", now = Date.now()): string | null {
  if (!config?.paid_generation_enabled) return "Paid generation is disabled until an operator approves it.";
  if (!config.provider_configured) return "The provider is not configured.";
  if (!estimate) return "Estimate this clip first.";
  const expiry = new Date(estimate.expires_at).getTime();
  if (!Number.isFinite(expiry) || expiry <= now) return "This estimate expired. Request a new estimate.";
  const approved = Number(cap), ceiling = Number(budget), reserved = Number(committed);
  const total = estimate.total_usd === null ? null : Number(estimate.total_usd);
  // Compare individual amounts at the database's six-decimal money precision.
  // Binary subtraction (0.3 - 0.1) must not incorrectly reject a $0.2 estimate.
  const units = (value: number) => Math.round(value * 1_000_000);
  if (!Number.isFinite(approved) || !Number.isFinite(reserved) || units(approved) <= units(reserved) || (total !== null && (!Number.isFinite(total) || units(approved) - units(reserved) < units(total)))) return "The remaining project cap must cover this estimate.";
  if (!Number.isFinite(ceiling) || approved > ceiling) return "The spending cap cannot exceed the project budget.";
  if (!budgetApproved) return "Approve the spending cap before generating.";
  if ((!estimate.pricing_verified || estimate.total_usd === null) && !priceApproved) return "Acknowledge the unavailable or unverified pricing.";
  return null;
}
