export type User = { id: string; email: string; role: "admin" | "editor" | "viewer" };
export type Project = {
  id: string; owner_id: string; name: string; prompt: string; status: string;
  target_duration_seconds: number; budget_usd: string; committed_cost_usd: string;
  approved_cap_usd: string | null; archived?: boolean;
};
export type VideoModel = {
  model_id: string; display_name: string; provider: string; endpoint_id: string;
  supported_generation_modes: string[]; supported_resolutions: string[];
  supported_aspect_ratios: string[]; min_duration_seconds: number; max_duration_seconds: number;
  supported_duration_seconds?: number[];
  supports_reference_images: boolean; supports_audio: boolean; enabled: boolean;
  is_default: boolean; price_usd: string | null; pricing_unit: string | null;
  pricing_details: {
    public_rates?: Record<string, string>; audio_control?: "always_on" | "selectable";
    aspect_ratio_source?: string; resolution_control?: string;
    supported_duration_seconds?: number[]; [key: string]: unknown;
  };
  documentation_url: string | null; verified_at: string | null; availability_reason?: string | null;
};
export type GenerationConfig = {
  paid_generation_enabled: boolean; provider_configured: boolean; requires_operator_approval: boolean;
};
export type ClipSettings = {
  model_id: string; duration_seconds: number; resolution: string; aspect_ratio: string;
  mode: string; audio_enabled: boolean;
};
export type Estimate = {
  id: string; base_usd: string | null; total_usd: string | null; pricing_verified: boolean;
  source: string; expires_at: string; request: Record<string, unknown>; scene_id?: string | null;
};
export type GenerationJob = {
  id: string; scene_id: string; scene_version_id: string; model_id: string;
  status: string; last_error_code: string | null; estimated_cost_usd: string | null;
  reserved_cost_usd: string | null; actual_cost_usd: string | null;
  output_asset_id: string | null; download_url?: string | null; no_longer_wanted: boolean;
  dispatch_state?: string;
};
