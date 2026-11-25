"use client";

import { money } from "@/lib/api";
import { publicClipPrice, settingsForModel } from "@/lib/model-settings";
import type { ClipSettings, VideoModel } from "@/lib/types";
import { Icon } from "./ui";

export function modelDocs(url: string | null) {
  try { const parsed = new URL(url ?? ""); return parsed.protocol === "https:" && parsed.hostname === "fal.ai" && !parsed.username && !parsed.password ? parsed.href : null; } catch { return null; }
}

export function ModelSelector({ models, settings, onChange, allowedAspectRatios }: { models: VideoModel[]; settings: ClipSettings; onChange: (value: ClipSettings) => void; allowedAspectRatios?: string[] }) {
  const selected = models.find(model => model.model_id === settings.model_id);
  return <>
    <fieldset className="model-options"><legend>Choose your video model</legend><p className="field-help">Each model has its own capabilities. Settings follow your selection.</p><div className="model-option-grid">{models.map(model => {
      const active = model.model_id === settings.model_id;
      const price = publicClipPrice(model, settingsForModel(model, settings));
      return <label key={model.model_id} className={`model-option ${active ? "model-option-active" : ""} ${!model.enabled ? "model-option-disabled" : ""}`}>
        <div className="model-option-top"><span className="model-symbol"><Icon name="models" size={20}/></span><input type="radio" name="video-model" aria-label={model.display_name} checked={active} disabled={!model.enabled} onChange={() => onChange(settingsForModel(model, settings))}/></div>
        <strong>{model.display_name}</strong><span className="model-provider">{model.provider} · {model.supported_generation_modes.includes("text_to_video") ? "Text to video" : "Image to video"}</span>
        <div className="model-capabilities"><span>{model.supported_resolutions.join(" / ")}</span><span>{model.min_duration_seconds}–{model.max_duration_seconds}s</span></div>
        <div className="model-option-price">{price === null ? "Price unavailable" : `${money(price)} estimated / clip`}<span>Public pricing · unverified</span></div>
        {!model.enabled && <span className="availability-note">{model.availability_reason ?? "Not available for generation"}</span>}
      </label>;
    })}</div></fieldset>
    {selected && <>
      <div className="model-setting-grid"><label>Resolution<select value={settings.resolution} onChange={e => onChange({ ...settings, resolution: e.target.value })}>{selected.supported_resolutions.map(value => <option key={value} value={value}>{value}{selected.pricing_details.resolution_control === "fixed" ? " · fixed model output" : ""}</option>)}</select></label><label>Clip duration<select value={settings.duration_seconds} onChange={e => onChange({ ...settings, duration_seconds: Number(e.target.value) })}>{(selected.supported_duration_seconds ?? selected.pricing_details.supported_duration_seconds ?? Array.from({ length: selected.max_duration_seconds - selected.min_duration_seconds + 1 }, (_, index) => selected.min_duration_seconds + index)).map(value => <option key={value} value={value}>{value} seconds</option>)}</select></label><label>Aspect ratio<select value={settings.aspect_ratio} onChange={e => onChange({ ...settings, aspect_ratio: e.target.value })}>{selected.supported_aspect_ratios.filter(value => !allowedAspectRatios || allowedAspectRatios.includes(value)).map(value => <option key={value} value={value}>{value}{value === "16:9" ? " · landscape" : value === "9:16" ? " · portrait" : value === "1:1" ? " · square" : ""}</option>)}</select></label><label>Generation mode<select value={settings.mode} onChange={e => onChange({ ...settings, mode: e.target.value })}>{selected.supported_generation_modes.map(value => <option key={value} value={value}>{value === "text_to_video" ? "Text to video" : "Image to video"}</option>)}</select></label></div>
      <label className="checkbox-row"><input type="checkbox" checked={settings.audio_enabled} disabled={!selected.supports_audio || selected.pricing_details.audio_control === "always_on"} onChange={e => onChange({ ...settings, audio_enabled: e.target.checked })}/><span><strong>Native model audio</strong><small>{selected.pricing_details.audio_control === "always_on" ? "This model always generates audio; its API does not provide an off switch." : selected.supports_audio ? "Include native sound. This changes the provider price." : "This model does not support native audio."}</small></span></label>
      <div className="model-source">{modelDocs(selected.documentation_url) && <a href={modelDocs(selected.documentation_url)!} target="_blank" rel="noreferrer">Official model documentation<Icon name="arrow" size={13}/></a>}{selected.verified_at && <span>Catalog verified {new Date(selected.verified_at).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })}</span>}</div>
    </>}
  </>;
}
