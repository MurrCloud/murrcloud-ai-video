"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "@/lib/api";
import { defaultModel } from "@/lib/model-settings";
import { boardPayload, validateUpload } from "@/lib/storyboard";
import type { ProjectInput, Storyboard } from "@/lib/storyboard";
import type { Project, VideoModel } from "@/lib/types";
import { useUser } from "./studio-shell";
import { Icon, Notice } from "./ui";

export function PlanningProject() {
  const router = useRouter();
  const user = useUser();
  const [models, setModels] = useState<VideoModel[]>([]);
  const [modelId, setModelId] = useState("");
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const [kind, setKind] = useState<"prompt" | "script">("prompt");
  const [file, setFile] = useState<File | null>(null);
  const [duration, setDuration] = useState(60);
  const [language, setLanguage] = useState<"en" | "zh-CN">("en");
  const [style, setStyle] = useState("cinematic");
  const [audience, setAudience] = useState("");
  const [budget, setBudget] = useState("10.00");
  const [saved, setSaved] = useState<Project | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const docxPicker = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let active = true;
    api<VideoModel[]>("/models").then(catalog => {
      if (!active) return;
      const available = catalog.filter(model => model.enabled && model.supported_generation_modes.includes("text_to_video"));
      setModels(available); setModelId(defaultModel(available)?.model_id ?? "");
    }).catch(error => { if (active) setError(errorMessage(error)); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);

  async function create(event: React.FormEvent) {
    event.preventDefault(); setError("");
    if (!file && !text.trim()) { setError("Add a prompt, a script, or a DOCX document."); return; }
    if (file) { const invalid = validateUpload(file, "docx"); if (invalid) { setError(invalid); return; } }
    setBusy(true);
    try {
      let project = saved;
      if (!project) {
        project = await api<Project>("/projects", { method: "POST", body: JSON.stringify({ name, prompt: file ? "" : text.slice(0, 10000), target_duration_seconds: duration, budget_usd: budget, aspect_ratio: "16:9", language }) });
        setSaved(project);
      }
      if (file) {
        const body = new FormData(); body.append("file", file);
        await api<ProjectInput>(`/projects/${project.id}/imports/docx`, { method: "POST", body });
      } else {
        await api<ProjectInput>(`/projects/${project.id}/inputs`, { method: "POST", body: JSON.stringify({ kind, text, source_name: kind === "script" ? "Text script" : "Text prompt" }) });
      }
      // Saving input increments the server revision. Always read it before PUT.
      const board = await api<Storyboard>(`/projects/${project.id}/storyboard`);
      await api<Storyboard>(`/projects/${project.id}/storyboard`, { method: "PUT", body: JSON.stringify(boardPayload({ ...board, title: name, target_duration_seconds: duration, language, visual_style: style, target_audience: audience, default_model_id: modelId || null })) });
      router.push(`/projects/${project.id}/storyboard`);
    } catch (error) { setError(errorMessage(error)); setBusy(false); }
  }

  if (user?.role === "viewer") return <Notice>Your account can review plans. An editor or administrator can create a storyboard.</Notice>;
  return <>
    <div className="breadcrumb"><Link href="/">Workspace</Link><span>/</span><span>Plan a video</span></div>
    <div className="page-heading"><div><span className="eyebrow">FROM SCRIPT TO PRODUCTION PLAN</span><h1>Plan your next video</h1><p>Import a script, set your production goals, and shape the storyboard.</p></div></div>
    <Notice>Saving a project or importing a document does not call an AI provider. Manual storyboard editing works without API keys.</Notice>
    {error && <Notice kind="error">{error}</Notice>}
    {saved && !busy && <Notice>Project saved. You can retry the input import without creating another project.</Notice>}
    <form className="planning-form" onSubmit={create}>
      <fieldset className="request-fields" disabled={busy || loading}>
        <section className="panel"><div className="panel-heading"><span className="step-number">01</span><div><h2>Your story starts here</h2><p>Use a short idea or a complete English or Chinese script.</p></div></div>
          <label>Project name<input value={name} required maxLength={160} onChange={event => setName(event.target.value)} placeholder="e.g. A new chapter for MurrCloud"/></label>
          <label>Input type<select value={kind} onChange={event => setKind(event.target.value as "prompt" | "script")} disabled={Boolean(file)}><option value="prompt">Text prompt</option><option value="script">Full script</option></select></label>
          <label>{kind === "script" ? "Production script" : "Video prompt"}<textarea rows={8} maxLength={100000} value={text} disabled={Boolean(file)} onChange={event => setText(event.target.value)} placeholder="Describe the story, the audience and what you want the video to communicate…"/><span className="field-help">Up to 100,000 characters. Your complete source text is saved for storyboard planning.</span></label>
          <div className="upload-box"><Icon name="download" size={22}/><label>Or import a DOCX script<input ref={docxPicker} type="file" accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document" onChange={event => { const selected = event.target.files?.[0]; if (!selected) { setFile(null); return; } const invalid = validateUpload(selected, "docx"); if (invalid) { setError(invalid); event.target.value = ""; setFile(null); } else { setFile(selected); setError(""); } }}/></label><p>Maximum 5 MiB. Extracted text is saved for review; the original document is not retained.</p>{file && <div className="selected-file"><strong>{file.name}</strong><button type="button" className="text-button" onClick={() => { setFile(null); if (docxPicker.current) docxPicker.current.value = ""; }}>Use text instead</button></div>}</div>
        </section>
        <section className="panel"><div className="panel-heading"><span className="step-number">02</span><div><h2>Production direction</h2><p>These settings can be refined in the storyboard editor.</p></div></div>
          <div className="form-grid"><label>Target duration (seconds)<input type="number" required min={5} max={600} step={1} value={duration} onChange={event => setDuration(Number(event.target.value))}/></label><label>Narration language<select value={language} onChange={event => setLanguage(event.target.value as "en" | "zh-CN")}><option value="en">English</option><option value="zh-CN">简体中文</option></select></label><label>Visual style<input maxLength={200} value={style} onChange={event => setStyle(event.target.value)}/></label><label>Target audience<input maxLength={500} value={audience} onChange={event => setAudience(event.target.value)} placeholder="Who should this video reach?"/></label></div>
          <label>Default video model<select value={modelId} onChange={event => setModelId(event.target.value)}><option value="">Choose a model</option>{models.map(model => <option key={model.model_id} value={model.model_id}>{model.display_name}</option>)}</select><span className="field-help">Individual scenes can use different enabled models.</span></label>
          <label>Project budget (USD)<input type="number" required min="0.0001" max="10000" step="0.0001" disabled={Boolean(saved)} value={budget} onChange={event => setBudget(event.target.value)}/><span className="field-help">A budget is not spending approval. AI planning and video generation require separate explicit approval.</span></label>
          <button className="button button-primary" type="submit" disabled={busy || loading || (!file && !text.trim())}>{busy ? "Preparing your project…" : <><Icon name="arrow" size={17}/>Save & open storyboard</>}</button>
        </section>
      </fieldset>
    </form>
  </>;
}
