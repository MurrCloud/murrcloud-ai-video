"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { api, errorMessage, money } from "@/lib/api";
import { defaultModel, publicClipPrice, settingsForModel, validateSettings } from "@/lib/model-settings";
import type { ClipSettings, Estimate, GenerationConfig, GenerationJob, Project, VideoModel } from "@/lib/types";
import { ApprovalPanel } from "./approval-panel";
import { ModelSelector } from "./model-selector";
import { useUser } from "./studio-shell";
import { Icon, Notice } from "./ui";

const initialSettings: ClipSettings = { model_id: "", duration_seconds: 5, resolution: "", aspect_ratio: "16:9", mode: "text_to_video", audio_enabled: false };

export function NewProject() {
  const router = useRouter();
  const search = useSearchParams();
  const user = useUser();
  const existingId = search.get("project");
  const sceneId = search.get("scene");
  const queryModel = search.get("model");
  const [models, setModels] = useState<VideoModel[]>([]);
  const [config, setConfig] = useState<GenerationConfig | null>(null);
  const [settings, setSettings] = useState<ClipSettings>(initialSettings);
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [negativePrompt, setNegativePrompt] = useState("");
  const [budget, setBudget] = useState("10.00");
  const [language, setLanguage] = useState("en");
  const [allowance, setAllowance] = useState(20);
  const [project, setProject] = useState<Project | null>(null);
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [configError, setConfigError] = useState("");
  const idempotency = useRef<{ estimate: string; key: string } | null>(null);
  const selected = models.find(model => model.model_id === settings.model_id);

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const catalog = await api<VideoModel[]>("/models");
        if (!active) return;
        setModels(catalog);
        const chosen = catalog.find(model => model.enabled && model.model_id === queryModel) ?? defaultModel(catalog);
        if (chosen) setSettings(settingsForModel(chosen));
        if (existingId) {
          const saved = await api<Project>(`/projects/${encodeURIComponent(existingId)}`);
          if (!active) return;
          setProject(saved); setName(saved.name); setPrompt(saved.prompt); setBudget(saved.budget_usd);
        }
        try { const gate = await api<GenerationConfig>("/generation-config"); if (active) setConfig(gate); }
        catch { if (active) setConfigError("Generation configuration could not be verified. Paid submission remains disabled."); }
      } catch (error) { if (active) setError(errorMessage(error)); }
      finally { if (active) setLoading(false); }
    }
    void load();
    return () => { active = false; };
  }, [existingId, queryModel]);

  function invalidate() { setEstimate(null); idempotency.current = null; }
  function changeSettings(value: ClipSettings) { setSettings(value); invalidate(); }

  async function estimateClip(event: React.FormEvent) {
    event.preventDefault(); setError("");
    const invalid = validateSettings(selected, settings);
    if (invalid) { setError(invalid); return; }
    if (!prompt.trim()) { setError("Add a visual prompt for your clip."); return; }
    setBusy(true); setEstimate(null);
    try {
      let saved = project;
      if (!saved) {
        saved = await api<Project>("/projects", { method: "POST", body: JSON.stringify({ name, prompt, target_duration_seconds: Math.max(5, settings.duration_seconds), budget_usd: budget, aspect_ratio: ["16:9", "9:16", "1:1"].includes(settings.aspect_ratio) ? settings.aspect_ratio : "16:9", language }) });
        setProject(saved);
      }
      const quote = await api<Estimate>(`/projects/${saved.id}/generations/estimate`, { method: "POST", body: JSON.stringify({ ...settings, prompt, negative_prompt: negativePrompt, retry_allowance_percent: allowance, ...(sceneId ? { scene_id: sceneId } : {}) }) });
      setEstimate(quote); idempotency.current = { estimate: quote.id, key: crypto.randomUUID() };
    } catch (error) { setError(errorMessage(error)); }
    finally { setBusy(false); }
  }

  async function approve(cap: string, approvePrice: boolean) {
    if (!project || !estimate || !config?.paid_generation_enabled || !config.provider_configured) return;
    setBusy(true); setError("");
    if (!idempotency.current || idempotency.current.estimate !== estimate.id) idempotency.current = { estimate: estimate.id, key: crypto.randomUUID() };
    try {
      await api<GenerationJob>(`/projects/${project.id}/generations`, { method: "POST", body: JSON.stringify({ estimate_id: estimate.id, idempotency_key: idempotency.current.key, approved_cap_usd: cap, approve_unverified_price: approvePrice }) });
      router.push(`/projects/${project.id}`);
    } catch (error) { setError(errorMessage(error)); setBusy(false); }
  }

  if (loading) return <div className="loading-panel" role="status"><span className="spinner"/>Loading verified model capabilities…</div>;
  if (user?.role === "viewer") return <Notice>Your account can review projects. An editor or administrator can create generations.</Notice>;
  const clipPrice = selected ? publicClipPrice(selected, settings) : null;
  return <>
    <div className="breadcrumb"><Link href="/">Workspace</Link><span>/</span><span>{sceneId ? "Regenerate scene" : "New video project"}</span></div>
    <div className="page-heading"><div><span className="eyebrow">GIVE YOUR IDEA A FIRST FRAME</span><h1>{sceneId ? "A new take on your scene" : "Create a video project"}</h1><p>Select a model and review a clip estimate before generation.</p></div><span className="subtle-badge"><Icon name="film" size={14}/>CLOUD CLIP GENERATION</span></div>
    {error && <Notice kind="error">{error}</Notice>}{configError && <Notice kind="error">{configError}</Notice>}
    {!config?.paid_generation_enabled && <Notice><strong>Safe preparation mode.</strong> You can save projects and estimate clips. Paid generation remains disabled until operator approval.</Notice>}
    {sceneId && <Notice>Regeneration creates a new scene version. Previously approved versions and their media are preserved.</Notice>}
    <div className="creation-grid"><div className="creation-main"><form onSubmit={estimateClip}><fieldset className="request-fields" disabled={busy}>
      <section className="panel"><div className="panel-heading"><span className="step-number">01</span><div><h2>Set the scene</h2><p>A clear brief gives your production a strong start.</p></div></div><label>Project name<input value={name} onChange={e => setName(e.target.value)} required minLength={1} maxLength={160} disabled={Boolean(project)} placeholder="e.g. Autumn product launch"/></label><label>Visual prompt<textarea value={prompt} onChange={e => { setPrompt(e.target.value); invalidate(); }} required rows={5} maxLength={2500} placeholder="Describe the subject, setting, action, camera movement and visual style…"/><span className="field-help">Describe one clip. {prompt.length.toLocaleString()} / 2,500 characters</span></label><div className="form-grid"><label>Project budget (USD)<input type="number" min="0.0001" max="10000" step="0.0001" required value={budget} disabled={Boolean(project)} onChange={e => setBudget(e.target.value)}/></label><label>Project language<select value={language} disabled={Boolean(project)} onChange={e => setLanguage(e.target.value)}><option value="en">English</option><option value="zh-CN">简体中文</option></select></label></div>{project && <p className="field-help saved-note"><Icon name="check" size={14}/>Project saved. Clip settings can still be changed before approval.</p>}</section>
      <section className="panel"><div className="panel-heading"><span className="step-number">02</span><div><h2>Choose your engine</h2><p>Find the right balance of motion, detail and cost.</p></div></div>{models.length ? <ModelSelector models={models} settings={settings} onChange={changeSettings} allowedAspectRatios={["16:9", "9:16", "1:1"]}/> : <Notice>No models are available. Ask an administrator to seed the verified catalog.</Notice>}<details className="advanced-settings"><summary>Advanced clip settings</summary><label>Negative prompt<textarea value={negativePrompt} maxLength={500} rows={2} onChange={e => { setNegativePrompt(e.target.value); invalidate(); }} placeholder="Describe details to avoid…"/></label><label>Estimate retry allowance (%)<input type="number" value={allowance} min="0" max="100" step="1" onChange={e => { setAllowance(Number(e.target.value)); invalidate(); }}/><span className="field-help">Adds estimate headroom. Paid submissions are never automatically retried.</span></label></details><div className="estimate-action"><span><strong>{clipPrice === null ? "Price unavailable" : `From ${money(clipPrice)} / clip`}</strong><small>Public estimate · final account pricing may differ</small></span><button className="button button-primary" type="submit" disabled={busy || Boolean(validateSettings(selected, settings))}>{busy ? "Preparing estimate…" : <><Icon name="spark" size={17}/>{estimate ? "Refresh estimate" : "Save & estimate clip"}</>}</button></div></section>
    </fieldset></form>{estimate && project && <ApprovalPanel key={estimate.id} estimate={estimate} config={config} project={project} busy={busy} onApprove={approve}/>}</div>
    <aside className="creation-sidebar"><div className="brief-card"><span className="eyebrow">YOUR PRODUCTION BRIEF</span><div className={`aspect-preview ${settings.aspect_ratio === "9:16" ? "preview-portrait" : settings.aspect_ratio === "1:1" ? "preview-square" : ""}`}><span className="preview-corner corner-tl"/><span className="preview-corner corner-br"/><Icon name="film" size={34}/><span>YOUR NEXT CLIP</span></div><h3>{name || "Untitled project"}</h3><dl><div><dt>Model</dt><dd>{selected?.display_name ?? "Choose a model"}</dd></div><div><dt>Format</dt><dd>{settings.aspect_ratio || "—"} · {settings.resolution || "—"}</dd></div><div><dt>Clip length</dt><dd>{settings.duration_seconds} seconds</dd></div><div><dt>Native audio</dt><dd>{settings.audio_enabled ? "Enabled" : "Disabled"}</dd></div><div><dt>Project budget</dt><dd>{money(budget)}</dd></div></dl></div><div className="process-card"><Icon name="lock" size={22}/><h3>You're in control</h3><p>Saving a project or requesting an estimate does not start a paid generation.</p><ol><li>Configure your clip</li><li>Review the estimate</li><li>Approve spending</li><li>Track and download</li></ol></div>{project && <Link className="text-link" href={`/projects/${project.id}`}>Open saved project<Icon name="arrow" size={16}/></Link>}</aside></div>
  </>;
}
