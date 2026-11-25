"use client";

import Link from "next/link";
import { changeSceneModel, newScene, reorderScenes } from "@/lib/storyboard";
import type { ReferenceImage, Storyboard, StoryboardScene } from "@/lib/storyboard";
import type { VideoModel } from "@/lib/types";
import { EmptyState, Icon, Notice } from "./ui";

export function StoryboardEditor({ board, models, references, disabled = false, onChange, onEstimateScene, sceneActionsDisabled = false }: {
  board: Storyboard; models: VideoModel[]; references: ReferenceImage[];
  disabled?: boolean; onChange: (value: Storyboard) => void;
  onEstimateScene?: (scene: StoryboardScene) => void; sceneActionsDisabled?: boolean;
}) {
  const available = models.filter(model => model.enabled && model.supported_generation_modes.includes("text_to_video"));
  const defaultModel = available.find(model => model.model_id === board.default_model_id) ?? available[0];
  const total = board.scenes.reduce((sum, scene) => sum + scene.duration_seconds, 0);

  function updateScene(index: number, changes: Partial<StoryboardScene>) {
    onChange({ ...board, scenes: board.scenes.map((scene, position) => position === index ? { ...scene, ...changes, status: "draft" } : scene) });
  }

  function removeScene(index: number) {
    onChange({ ...board, scenes: board.scenes.filter((_, position) => position !== index).map((scene, order) => ({ ...scene, order: order + 1 })) });
  }

  return <div className="storyboard-editor">
    <fieldset className="request-fields" disabled={disabled}>
      <section className="panel storyboard-settings" aria-label="Storyboard settings">
        <div className="panel-heading"><span className="step-number">01</span><div><h2>Production plan</h2><p>Set the format, audience and timing for the whole video.</p></div></div>
        <label>Storyboard title<input value={board.title} maxLength={200} onChange={event => onChange({ ...board, title: event.target.value })}/></label>
        <div className="form-grid">
          <label>Target duration (seconds)<input type="number" min={5} max={600} step={1} value={board.target_duration_seconds} onChange={event => onChange({ ...board, target_duration_seconds: Number(event.target.value) })}/></label>
          <label>Narration language<select value={board.language} onChange={event => onChange({ ...board, language: event.target.value as "en" | "zh-CN" })}><option value="en">English</option><option value="zh-CN">简体中文</option></select></label>
          <label>Visual style<input value={board.visual_style} maxLength={200} onChange={event => onChange({ ...board, visual_style: event.target.value })} placeholder="e.g. warm cinematic documentary"/></label>
          <label>Target audience<input value={board.target_audience} maxLength={500} onChange={event => onChange({ ...board, target_audience: event.target.value })} placeholder="Who is this video for?"/></label>
        </div>
        <label>Default video model<select value={board.default_model_id ?? ""} onChange={event => onChange({ ...board, default_model_id: event.target.value })}><option value="" disabled>Choose a model</option>{available.map(model => <option key={model.model_id} value={model.model_id}>{model.display_name}</option>)}</select><span className="field-help">Used for new scenes. Existing scenes keep their individual model selections.</span></label>
        <div className="duration-meter" aria-label="Storyboard duration"><strong>{total}s <span>/ {board.target_duration_seconds}s target</span></strong><span>{board.scenes.length} scene{board.scenes.length === 1 ? "" : "s"} · validate to check timing tolerance</span></div>
      </section>
      <div className="section-heading"><div><h2>Shape every scene</h2><p>Reorder, refine or replace a scene before approving generation.</p></div><button type="button" className="button button-secondary button-small" disabled={!defaultModel || board.scenes.length >= 120} onClick={() => defaultModel && onChange({ ...board, scenes: [...board.scenes, newScene(defaultModel, board.scenes.length + 1)] })}><Icon name="plus" size={16}/>Add scene</button></div>
      {!available.length && <Notice>No enabled text-to-video model is available. An administrator must configure the catalog before scenes can be generated.</Notice>}
      {!board.scenes.length && <div className="panel"><EmptyState title="A blank canvas for your production">Add scenes manually, or use the AI Director after an operator enables it. Nothing is generated or charged by editing this plan.</EmptyState></div>}
      <div className="storyboard-scenes">
        {board.scenes.map((scene, index) => {
          const model = models.find(item => item.model_id === scene.video_model_id);
          const durationOptions = model?.supported_duration_seconds ?? model?.pricing_details.supported_duration_seconds ?? (model ? Array.from({ length: model.max_duration_seconds - model.min_duration_seconds + 1 }, (_, offset) => model.min_duration_seconds + offset) : []);
          const selectedReferences = references.filter(reference => scene.reference_asset_ids.includes(reference.id));
          return <article className="panel storyboard-scene" key={scene.id ?? scene.scene_id} aria-label={`Scene ${index + 1}`}>
            <div className="scene-card-heading"><div className="scene-number">{String(index + 1).padStart(2, "0")}</div><div><h3>Scene {index + 1}</h3><span>{scene.duration_seconds}s · {model?.display_name ?? "Unavailable model"}</span></div><div className="scene-order-controls"><button type="button" className="button button-secondary button-small" disabled={index === 0} aria-label={`Move scene ${index + 1} up`} onClick={() => onChange({ ...board, scenes: reorderScenes(board.scenes, index, index - 1) })}>↑</button><button type="button" className="button button-secondary button-small" disabled={index === board.scenes.length - 1} aria-label={`Move scene ${index + 1} down`} onClick={() => onChange({ ...board, scenes: reorderScenes(board.scenes, index, index + 1) })}>↓</button><button type="button" className="text-button danger-text" aria-label={`Remove scene ${index + 1}`} onClick={() => removeScene(index)}>Remove</button></div></div>
            <label>Video model<select aria-label={`Video model for scene ${index + 1}`} value={scene.video_model_id} onChange={event => { const next = models.find(item => item.model_id === event.target.value); if (next) updateScene(index, changeSceneModel(scene, next)); }}>{!available.some(item => item.model_id === scene.video_model_id) && <option value={scene.video_model_id}>Unavailable model</option>}{available.map(item => <option key={item.model_id} value={item.model_id}>{item.display_name} · {item.provider}</option>)}</select></label>
            <div className="model-setting-grid scene-setting-grid">
              <label>Duration<select aria-label={`Duration for scene ${index + 1}`} value={scene.duration_seconds} onChange={event => updateScene(index, { duration_seconds: Number(event.target.value) })}>{!durationOptions.includes(scene.duration_seconds) && <option value={scene.duration_seconds}>{scene.duration_seconds}s · unsupported</option>}{durationOptions.map(value => <option key={value} value={value}>{value} seconds</option>)}</select></label>
              <label>Resolution<select aria-label={`Resolution for scene ${index + 1}`} value={scene.resolution} onChange={event => updateScene(index, { resolution: event.target.value })}>{!model?.supported_resolutions.includes(scene.resolution) && <option value={scene.resolution}>{scene.resolution || "Choose"} · unsupported</option>}{model?.supported_resolutions.map(value => <option key={value} value={value}>{value}</option>)}</select></label>
              <label>Aspect ratio<select aria-label={`Aspect ratio for scene ${index + 1}`} value={scene.aspect_ratio} onChange={event => updateScene(index, { aspect_ratio: event.target.value })}>{!model?.supported_aspect_ratios.includes(scene.aspect_ratio) && <option value={scene.aspect_ratio}>{scene.aspect_ratio || "Choose"} · unsupported</option>}{model?.supported_aspect_ratios.map(value => <option key={value} value={value}>{value}</option>)}</select></label>
              <label>Transition<select aria-label={`Transition for scene ${index + 1}`} value={scene.transition} onChange={event => updateScene(index, { transition: event.target.value as StoryboardScene["transition"] })}><option value="fade">Fade</option><option value="cut">Cut</option><option value="dissolve">Dissolve</option></select></label>
            </div>
            <label className="checkbox-row"><input type="checkbox" aria-label={`Native audio for scene ${index + 1}`} checked={scene.audio_enabled} disabled={!model?.supports_audio || model.pricing_details.audio_control === "always_on"} onChange={event => updateScene(index, { audio_enabled: event.target.checked })}/><span>Native model audio{model?.pricing_details.audio_control === "always_on" && <small>Always enabled by this model’s API.</small>}</span></label>
            <label>Visual prompt<textarea aria-label={`Visual prompt for scene ${index + 1}`} value={scene.visual_prompt} rows={4} maxLength={5000} onChange={event => updateScene(index, { visual_prompt: event.target.value })}/></label>
            <label>Narration<textarea aria-label={`Narration for scene ${index + 1}`} value={scene.narration} rows={3} maxLength={5000} onChange={event => updateScene(index, { narration: event.target.value })}/><span className="field-help">Narration text is editable here. Audio synthesis and subtitles arrive in the production pipeline.</span></label>
            <details className="advanced-settings"><summary>Negative prompt and planning references</summary><label>Negative prompt<textarea aria-label={`Negative prompt for scene ${index + 1}`} value={scene.negative_prompt} maxLength={model?.model_id.includes("wan") ? 500 : 2500} rows={2} onChange={event => updateScene(index, { negative_prompt: event.target.value })}/></label><fieldset className="reference-choices"><legend>Reference images for scene {index + 1}</legend><p className="field-help">Planning references help you review the scene. Image pixels are not sent to the Director or text-to-video provider in this phase.</p>{references.length ? references.map(reference => <label key={reference.id} className="checkbox-row"><input type="checkbox" checked={scene.reference_asset_ids.includes(reference.id)} disabled={!scene.reference_asset_ids.includes(reference.id) && scene.reference_asset_ids.length >= 10} onChange={event => updateScene(index, { reference_asset_ids: event.target.checked ? [...scene.reference_asset_ids, reference.id] : scene.reference_asset_ids.filter(id => id !== reference.id) })}/><span>{reference.metadata_json.caption || reference.original_filename || "Reference image"}</span></label>) : <p className="small muted">Upload a reference in the project assets panel first.</p>}</fieldset></details>
            {!!selectedReferences.length && <div className="scene-reference-thumbs">{selectedReferences.map(reference => <figure key={reference.id}><img src={`/api/v1/assets/${encodeURIComponent(reference.id)}/preview`} loading="lazy" alt={reference.metadata_json.caption || reference.original_filename || "Scene planning reference"}/><figcaption>{reference.metadata_json.caption || reference.original_filename}</figcaption></figure>)}</div>}
            {scene.reference_asset_ids.some(id => !references.some(reference => reference.id === id)) && <Notice kind="error">A saved reference image could not be loaded. Reload the assets before approving this scene.</Notice>}
            {scene.id && <div className="scene-generation-actions">{onEstimateScene && <button type="button" className="button button-secondary button-small" disabled={sceneActionsDisabled} onClick={() => onEstimateScene(scene)} aria-label={`Estimate generation for scene ${index + 1}`}>Estimate this scene</button>}<Link href={`/projects/${board.project_id}`} className="text-link">View clip versions<Icon name="arrow" size={14}/></Link></div>}
          </article>;
        })}
      </div>
    </fieldset>
  </div>;
}
