"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, errorMessage } from "@/lib/api";
import { initialSelections, likelyUpscaledScenes, outputDimensions, privateAssetLink, productionDuration, productionIssue, renderTerminal, selectionsComplete, validateMusic, validProductionSettings } from "@/lib/production";
import type { ProductionConfig, ProductionSettings, ProductionState, RenderJob, SceneSelections } from "@/lib/production";
import { useUser } from "./studio-shell";
import { EmptyState, Icon, Notice, StatusPill } from "./ui";

const same = (left: unknown, right: unknown) => JSON.stringify(left) === JSON.stringify(right);

export function ProductionWorkspace({ projectId }: { projectId: string }) {
  const user = useUser();
  const base = `/projects/${encodeURIComponent(projectId)}`;
  const [state, setState] = useState<ProductionState | null>(null);
  const canEdit = (user?.role === "editor" || user?.role === "admin") && !state?.archived;
  const [draft, setDraft] = useState<ProductionSettings | null>(null);
  const [config, setConfig] = useState<ProductionConfig | null>(null);
  const [selections, setSelections] = useState<SceneSelections>({});
  const [jobs, setJobs] = useState<RenderJob[]>([]);
  const [previewId, setPreviewId] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [conflict, setConflict] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const [musicFile, setMusicFile] = useState<File | null>(null);
  const musicPicker = useRef<HTMLInputElement>(null);
  const renderKey = useRef<{ fingerprint: string; key: string } | null>(null);

  const applyState = useCallback((value: ProductionState) => {
    setState(value); setDraft(value.settings); setSelections(initialSelections(value));
    setJobs(value.render_jobs); setConflict(false); setReviewed(false);
  }, []);
  const load = useCallback(async () => {
    const [production, capabilities] = await Promise.all([
      api<ProductionState>(`${base}/production`), api<ProductionConfig>("/production-config"),
    ]);
    applyState(production); setConfig(capabilities);
  }, [base, applyState]);
  useEffect(() => {
    let active = true;
    load().catch(error => { if (active) setError(errorMessage(error)); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [load]);
  useEffect(() => {
    if (!jobs.some(job => !renderTerminal.has(job.status))) return;
    let active = true;
    const timer = setInterval(async () => {
      if (document.visibilityState !== "visible") return;
      try {
        const next = await api<RenderJob[]>(`${base}/renders`);
        if (active) setJobs(next);
      } catch (error) { if (active) setError(errorMessage(error)); }
    }, 5000);
    return () => { active = false; clearInterval(timer); };
  }, [base, jobs]);
  const dirty = Boolean(state && draft && !same(state.settings, draft));
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  function edit(changes: Partial<ProductionSettings>) {
    setDraft(value => value ? { ...value, ...changes } : value);
    setReviewed(false); setMessage("");
  }
  function report(error: unknown) {
    if (error instanceof ApiError && error.status === 409 && error.message === "revision_conflict") setConflict(true);
    setError(productionIssue(errorMessage(error)));
  }
  async function operation(name: string, work: () => Promise<void>) {
    if (busy) return;
    setBusy(name); setError(""); setMessage("");
    try { await work(); } catch (error) { report(error); } finally { setBusy(null); }
  }
  async function save() {
    if (!state || !draft) return;
    await operation("save", async () => {
      const next = await api<ProductionState>(`${base}/production/settings`, {
        method: "PUT", body: JSON.stringify({ expected_revision: state.revision, settings: draft }),
      });
      applyState(next);
      setSelections(Object.fromEntries(next.scenes.map(scene => [scene.scene_id, scene.versions.some(version => version.scene_version_id === selections[scene.scene_id]) ? selections[scene.scene_id] : scene.selected_scene_version_id ?? ""])));
      setMessage("Production settings saved. Review the selected clips before rendering.");
    });
  }
  async function renderVideo(failedJob?: RenderJob) {
    if (!state || !draft || !config?.render_enabled || !canEdit || busy || dirty || conflict || !reviewed) return;
    if (failedJob && (failedJob.status !== "failed" || !failedJob.is_current || !jobs.some(job => job.id === failedJob.id && job.status === "failed" && job.is_current))) return;
    if (!selectionsComplete(state.scenes, selections) || !validProductionSettings(draft, config) || (draft.narration_enabled && !config.narration_enabled)) return;
    if (jobs.some(job => !renderTerminal.has(job.status)) || (likelyUpscaledScenes(state.scenes, selections, draft).length > 0 && !draft.allow_upscale)) return;
    const fingerprint = JSON.stringify({ revision: state.revision, selections });
    if (failedJob) renderKey.current = null;
    if (renderKey.current?.fingerprint !== fingerprint) renderKey.current = { fingerprint, key: crypto.randomUUID() };
    await operation("render", async () => {
      const job = await api<RenderJob>(`${base}/renders`, {
        method: "POST", body: JSON.stringify({ expected_revision: state.revision, idempotency_key: renderKey.current!.key, scene_versions: selections }),
      });
      renderKey.current = null; setReviewed(false);
      setJobs(value => [job, ...value.filter(existing => existing.id !== job.id)]);
      setMessage(job.status === "failed" ? "This production attempt failed. Review the error and settings before starting a new render attempt." : "The selected clip snapshot is queued for narration and rendering. No video generation request was submitted.");
    });
  }
  async function action(job: RenderJob, action: "dispatch" | "cancel") {
    await operation(job.id, async () => {
      await api(`${base}/renders/${encodeURIComponent(job.id)}/${action}`, { method: "POST" });
      setJobs(await api<RenderJob[]>(`${base}/renders`));
      setMessage(action === "cancel" ? "Cancellation requested for this local production job." : "The saved production job was queued for processing.");
    });
  }
  async function uploadMusic() {
    if (!musicFile || !state || !draft) return;
    await operation("music", async () => {
      const body = new FormData(); body.append("file", musicFile);
      const asset = await api<{ id: string; original_filename: string | null }>(`${base}/assets/music`, { method: "POST", body });
      setState(value => value ? { ...value, music_assets: [asset, ...value.music_assets.filter(existing => existing.id !== asset.id)] } : value);
      edit({ music_asset_id: asset.id }); setMusicFile(null);
      if (musicPicker.current) musicPicker.current.value = "";
      setMessage("Music uploaded privately. Save the production settings to use it.");
    });
  }

  if (loading) return <div className="loading-panel" role="status"><span className="spinner"/>Loading video production…</div>;
  if (!state || !draft) return <Notice kind="error">{error || "Production settings could not be loaded."}</Notice>;
  const dimensions = outputDimensions(draft.aspect_ratio);
  const voices = config?.voices.filter(voice => voice.language === draft.language) ?? [];
  const activeJob = jobs.some(job => !renderTerminal.has(job.status));
  const completeSelection = selectionsComplete(state.scenes, selections);
  const valid = validProductionSettings(draft, config);
  const upscaled = likelyUpscaledScenes(state.scenes, selections, draft);
  const currentPreview = jobs.find(job => job.id === previewId && job.status === "succeeded") ?? jobs.find(job => job.status === "succeeded");
  const previewUrl = privateAssetLink(currentPreview?.preview_url);
  const unavailable = !config?.render_enabled || (draft.narration_enabled && !config.narration_enabled);
  const blocked = !canEdit || Boolean(busy) || dirty || conflict || !valid || !completeSelection || !reviewed || unavailable || activeJob || (upscaled.length > 0 && !draft.allow_upscale);

  return <>
    {state.archived && <Notice>Archived projects are read-only. Restore the project from the <Link className="text-link" href="/library">project library</Link> before changing settings or rendering.</Notice>}
    <div className="breadcrumb"><Link href="/">Workspace</Link><span>/</span><Link href={`/projects/${projectId}`}>Project details</Link><span>/</span><span>Video production</span></div>
    <div className="page-heading"><div><span className="eyebrow">NARRATION · EDITING · FINAL VIDEO</span><h1>Bring your scenes together</h1><p>Choose retained clips, review sound and export a complete MP4.</p></div><Link href={`/projects/${projectId}/storyboard`} className="button button-secondary"><Icon name="film" size={17}/>Edit storyboard</Link></div>
    {error && <Notice kind="error">{error}</Notice>}{message && <Notice>{message}</Notice>}
    {!config?.render_enabled && <Notice>Rendering is disabled. You can prepare settings and review existing productions while an operator configures the isolated worker.</Notice>}
    {draft.narration_enabled && !config?.narration_enabled && <Notice>Narration is disabled. Turn narration off to render available clips without a speech-service request, or ask an operator to enable the adapter.</Notice>}
    {conflict && <Notice kind="error"><strong>A newer project revision exists.</strong> Your local production settings are preserved. Reload before saving or rendering.</Notice>}
    <div className="storyboard-toolbar"><div><strong>{dirty ? "Unsaved production settings" : `Saved project revision ${state.revision}`}</strong><span>Rendering keeps an immutable snapshot of every selected clip.</span></div><div className="toolbar-actions"><button type="button" className="button button-secondary button-small" disabled={Boolean(busy)} onClick={() => operation("reload", load)}>{dirty || conflict ? "Discard local changes & reload" : "Reload production"}</button>{canEdit && <button type="button" className="button button-primary button-small" disabled={!dirty || Boolean(busy) || conflict || !valid} onClick={save}>{busy === "save" ? "Saving…" : "Save production settings"}</button>}</div></div>

    <div className="production-layout"><div className="production-main">
      <section className="panel production-clips"><div className="panel-heading"><span className="step-number">01</span><div><h2>Select scene versions</h2><p>Regeneration keeps previous clips. Only the versions selected here enter this render.</p></div></div>
        {state.scenes.length ? state.scenes.map(scene => {
          const selected = scene.versions.find(version => version.scene_version_id === selections[scene.scene_id]);
          const clipPreview = privateAssetLink(selected?.preview_url);
          return <article className="production-scene" key={scene.scene_id}><div className="production-scene-title"><strong>Scene {scene.order} · {scene.scene_key}</strong><span>{scene.duration_seconds}s</span></div>
            <label>Clip version for scene {scene.order}<select value={selections[scene.scene_id] ?? ""} disabled={!canEdit || Boolean(busy)} onChange={event => { setSelections(value => ({ ...value, [scene.scene_id]: event.target.value })); setReviewed(false); }}><option value="">Choose a completed clip</option>{scene.versions.map(version => <option key={version.scene_version_id} value={version.scene_version_id}>Version {version.version_number}{version.width && version.height ? ` · ${version.width} × ${version.height}` : ""}</option>)}</select></label>
            {!scene.versions.length && <p className="production-missing">No completed clip is available. Generate this scene from the storyboard.</p>}
            {clipPreview && <video controls preload="none" src={clipPreview} aria-label={`Preview scene ${scene.order}`}/>}
            {scene.narration && <p className="production-narration"><strong>Narration</strong>{scene.narration}</p>}
            {canEdit && <Link href={`/projects/new?project=${encodeURIComponent(projectId)}&scene=${encodeURIComponent(scene.scene_id)}`} className="text-link">Regenerate scene {scene.order}<Icon name="refresh" size={14}/></Link>}
          </article>;
        }) : <EmptyState title="Build a storyboard first">Add and generate scenes before assembling your video.</EmptyState>}
      </section>

      <section className="panel production-preview"><div className="panel-heading"><span className="step-number">03</span><div><h2>Video preview</h2><p>Completed snapshots remain downloadable when later scenes or settings change.</p></div></div>
        {currentPreview && previewUrl ? <><label>Completed production<select value={currentPreview.id} onChange={event => setPreviewId(event.target.value)}>{jobs.filter(job => job.status === "succeeded").map((job, index) => <option value={job.id} key={job.id}>Production {jobs.filter(job => job.status === "succeeded").length - index} · {job.is_current ? "Current snapshot" : "Earlier snapshot"}</option>)}</select></label><video key={currentPreview.id} controls preload="metadata" src={previewUrl} aria-label="Completed video preview"/><div className="production-preview-actions"><span className="subtle-badge">{currentPreview.is_current ? "CURRENT PROJECT SNAPSHOT" : "HISTORICAL SNAPSHOT"}</span>{privateAssetLink(currentPreview.download_url) && <a className="button button-primary button-small" href={privateAssetLink(currentPreview.download_url)!}><Icon name="download" size={15}/>Download final MP4</a>}{privateAssetLink(currentPreview.srt_url) && <a className="button button-secondary button-small" href={privateAssetLink(currentPreview.srt_url)!}><Icon name="download" size={15}/>Download subtitles SRT</a>}</div></> : <EmptyState title="Your finished video will appear here">A production becomes available after media integrity and export checks pass.</EmptyState>}
      </section>
    </div><aside className="production-sidebar">
      <section className="panel production-settings"><div className="panel-heading"><span className="step-number">02</span><div><h2>Sound & export</h2><p>Settings apply to a new production snapshot.</p></div></div>
        <fieldset className="request-fields" disabled={!canEdit || Boolean(busy)}>
          <label>Output aspect ratio<select value={draft.aspect_ratio} onChange={event => edit({ aspect_ratio: event.target.value as ProductionSettings["aspect_ratio"] })}><option value="16:9">Landscape · 16:9</option><option value="9:16">Portrait · 9:16</option><option value="1:1">Square · 1:1</option></select></label>
          <div className="production-export-spec"><strong>{dimensions.width} × {dimensions.height}</strong><span>30 FPS · H.264 video · AAC audio</span><span>Expected duration {productionDuration(state.scenes, draft)}s, including title cards</span></div>
          <label className="checkbox-row"><input type="checkbox" checked={draft.allow_upscale} onChange={event => edit({ allow_upscale: event.target.checked })}/><span>I allow source footage below the output resolution to be upscaled.</span></label>
          {!!upscaled.length && <p className="production-upscale">The selected source is smaller than the output for {upscaled.map(scene => `scene ${scene.order}`).join(", ")}. {draft.allow_upscale ? "Upscaling is approved and will be recorded." : "Allow upscaling and save settings, or choose a larger source."} The renderer will verify every source.</p>}
          <label className="checkbox-row"><input type="checkbox" checked={draft.narration_enabled} onChange={event => edit({ narration_enabled: event.target.checked, ...(!event.target.checked ? { burn_subtitles: false } : {}) })}/><span>Generate narration from the scene script</span></label>
          <label>Narration language<select value={draft.language} disabled={!draft.narration_enabled} onChange={event => { const language = event.target.value as ProductionSettings["language"]; edit({ language, voice: config?.voices.find(voice => voice.language === language)?.id ?? "" }); }}><option value="en">English</option><option value="zh-CN">简体中文</option></select></label>
          <label>Narration voice<select value={draft.voice} disabled={!draft.narration_enabled} onChange={event => edit({ voice: event.target.value })}>{!voices.some(voice => voice.id === draft.voice) && <option value="">Choose a voice</option>}{voices.map(voice => <option key={voice.id} value={voice.id}>{voice.display_name}</option>)}</select></label>
          <label>Speaking rate<input type="number" min="0.5" max="2" step="0.05" value={draft.speaking_rate} disabled={!draft.narration_enabled} onChange={event => edit({ speaking_rate: event.target.value })}/></label>
          <label className="checkbox-row"><input type="checkbox" checked={draft.burn_subtitles} disabled={!draft.narration_enabled} onChange={event => edit({ burn_subtitles: event.target.checked })}/><span>Burn speech-aligned subtitles into the video</span></label>
          <p className="field-help">Edge speech is for internal evaluation. Availability is not guaranteed; an operator must review service terms before commercial use. Subtitles use returned speech timing.</p>
          <div className="production-setting-divider"/>
          <label>Opening title<input value={draft.opening_title} maxLength={200} onChange={event => edit({ opening_title: event.target.value })}/></label>
          <label>Closing title<input value={draft.closing_title} maxLength={200} onChange={event => edit({ closing_title: event.target.value })}/></label>
          <label>Seconds per title card<input type="number" min="1" max="5" step="1" value={draft.title_duration_seconds} onChange={event => edit({ title_duration_seconds: Number(event.target.value) })}/></label>
          <label>Background music<select value={draft.music_asset_id ?? ""} onChange={event => edit({ music_asset_id: event.target.value || null })}><option value="">No background music</option>{state.music_assets.map(asset => <option key={asset.id} value={asset.id}>{asset.original_filename || "Music asset"}</option>)}</select></label>
          <label>Music volume<input type="number" min="0" max="1" step="0.05" value={draft.music_volume} onChange={event => edit({ music_volume: event.target.value })}/></label>
          <label>Source clip audio volume<input type="number" min="0" max="1" step="0.05" value={draft.source_audio_volume} onChange={event => edit({ source_audio_volume: event.target.value })}/></label>
          <label>Logo watermark<select value={draft.watermark_asset_id ?? ""} onChange={event => edit({ watermark_asset_id: event.target.value || null })}><option value="">No watermark</option>{state.reference_assets.map((asset, index) => <option key={asset.id} value={asset.id}>{asset.original_filename || `Planning image ${index + 1}`}</option>)}</select></label>
          <p className="field-help">Upload a private logo through the storyboard reference library, then select it here.</p>
        </fieldset>
        <fieldset className="request-fields production-music-upload" disabled={!canEdit || Boolean(busy)}><label>Upload background music<input ref={musicPicker} type="file" accept=".mp3,.wav,audio/mpeg,audio/wav" onChange={event => { const file = event.target.files?.[0] ?? null; const invalid = file ? validateMusic(file, config?.max_music_upload_bytes ?? 5 * 1024 * 1024) : null; setError(invalid || ""); setMusicFile(invalid ? null : file); if (invalid) event.target.value = ""; }}/></label><button type="button" className="button button-secondary button-small" disabled={!musicFile} onClick={uploadMusic}>{busy === "music" ? "Uploading…" : "Upload private music"}</button><p className="field-help">MP3 or WAV, up to {Math.floor((config?.max_music_upload_bytes ?? 5 * 1024 * 1024) / 1024 / 1024)} MiB. Use music you have permission to include.</p></fieldset>
      </section>
      <section className="panel production-submit"><h2>Review & render</h2><p>Local production uses the selected clips and saved settings. It does not regenerate video scenes.</p>{!completeSelection && <p className="production-missing">Select a completed clip version for every scene.</p>}{dirty && <p className="field-help">Save production settings before reviewing this snapshot.</p>}<label className="checkbox-row"><input type="checkbox" checked={reviewed} disabled={!canEdit || dirty || conflict || !completeSelection || Boolean(busy)} onChange={event => setReviewed(event.target.checked)}/><span>I reviewed these clip versions, narration and saved export settings.</span></label><button type="button" className="button button-primary full-width" disabled={blocked} onClick={() => renderVideo()}>{busy === "render" ? "Queueing…" : !config?.render_enabled ? "Rendering disabled" : activeJob ? "Production in progress" : "Render reviewed video"}</button><p className="field-help">One video renders at a time. Host resource pressure pauses new processing; it does not trigger paid video submissions.</p></section>
    </aside></div>

    <div className="section-heading"><div><h2>Production history</h2><p>Progress and retained outputs for each immutable snapshot.</p></div><button type="button" className="button button-secondary button-small" disabled={Boolean(busy)} onClick={() => operation("history", async () => { setJobs(await api<RenderJob[]>(`${base}/renders`)); setReviewed(false); })}><Icon name="refresh" size={15}/>Refresh progress</button></div>
    {jobs.length ? <div className="production-history">{jobs.map(job => <article className="panel production-job" key={job.id}><div className="production-job-heading"><div><h3>Production {job.id.slice(0, 8)}</h3><span>{job.stage.replaceAll("_", " ")} · {job.is_current ? "Current snapshot" : "Earlier snapshot"}</span></div><StatusPill status={job.status}/></div><label className="production-progress">Progress<progress max="100" value={Math.min(100, Math.max(0, job.progress_percent))} aria-label={`Production ${job.id.slice(0, 8)} progress`}/><span>{Math.round(Math.min(100, Math.max(0, job.progress_percent)))}%</span></label>{job.error_code && <p className="job-error" role="alert">{productionIssue(job.error_code)}</p>}{job.warnings?.length > 0 && <ul className="production-warnings">{job.warnings.map((warning, index) => <li key={`${warning}:${index}`}>{productionIssue(warning)}</li>)}</ul>}<div className="job-actions">{job.status === "succeeded" && privateAssetLink(job.preview_url) && <button type="button" className="button button-secondary button-small" onClick={() => setPreviewId(job.id)}><Icon name="play" size={15}/>Preview this production</button>}{job.status === "succeeded" && privateAssetLink(job.download_url) && <a className="button button-primary button-small" href={privateAssetLink(job.download_url)!}><Icon name="download" size={15}/>Download MP4</a>}{job.status === "succeeded" && privateAssetLink(job.srt_url) && <a className="button button-secondary button-small" href={privateAssetLink(job.srt_url)!}>Download SRT</a>}{canEdit && job.status === "failed" && <button type="button" className="button button-secondary button-small" disabled={blocked || !job.is_current} onClick={() => renderVideo(job)}><Icon name="refresh" size={15}/>New render attempt</button>}{canEdit && !renderTerminal.has(job.status) && <button type="button" className="button button-secondary button-small" disabled={Boolean(busy)} onClick={() => action(job, "cancel")}>Cancel production</button>}{canEdit && ["pending", "paused"].includes(job.status) && config?.render_enabled && <button type="button" className="button button-secondary button-small" disabled={Boolean(busy)} onClick={() => action(job, "dispatch")}>Resume processing</button>}</div></article>)}</div> : <div className="panel"><EmptyState title="No productions yet">Save your export settings and review completed scene clips to prepare a render.</EmptyState></div>}
  </>;
}
