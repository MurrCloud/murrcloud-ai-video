"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, errorMessage, money } from "@/lib/api";
import { boardPayload, directorTerminal, sceneSettings, storyboardIssue, validateUpload } from "@/lib/storyboard";
import type { DirectorConfig, DirectorJob, ProjectInput, ReferenceImage, Storyboard, StoryboardEstimate, StoryboardScene, StoryboardValidation } from "@/lib/storyboard";
import type { Estimate, GenerationConfig, GenerationJob, Project, VideoModel } from "@/lib/types";
import { ApprovalPanel } from "./approval-panel";
import { StoryboardEditor } from "./storyboard-editor";
import { useUser } from "./studio-shell";
import { Icon, Notice, StatusPill } from "./ui";

const json = (value: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(value) });

export function StoryboardWorkspace({ projectId }: { projectId: string }) {
  const user = useUser();
  const [board, setBoard] = useState<Storyboard | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const canEdit = (user?.role === "admin" || user?.role === "editor") && !project?.archived;
  const [models, setModels] = useState<VideoModel[]>([]);
  const [inputs, setInputs] = useState<ProjectInput[]>([]);
  const [references, setReferences] = useState<ReferenceImage[]>([]);
  const [directorConfig, setDirectorConfig] = useState<DirectorConfig | null>(null);
  const [generationConfig, setGenerationConfig] = useState<GenerationConfig | null>(null);
  const [directorJobs, setDirectorJobs] = useState<DirectorJob[]>([]);
  const [sourceText, setSourceText] = useState("");
  const [sourceKind, setSourceKind] = useState<"prompt" | "script">("script");
  const [sourceSaved, setSourceSaved] = useState("");
  const [sourceSavedKind, setSourceSavedKind] = useState<"prompt" | "script">("script");
  const [dirty, setDirty] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [validation, setValidation] = useState<StoryboardValidation | null>(null);
  const [estimate, setEstimate] = useState<StoryboardEstimate | null>(null);
  const [sceneEstimate, setSceneEstimate] = useState<{ quote: Estimate; sceneKey: string } | null>(null);
  const [retryAllowance, setRetryAllowance] = useState(20);
  const [directorCap, setDirectorCap] = useState("1.00");
  const [directorApproved, setDirectorApproved] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [uploadCaption, setUploadCaption] = useState("");
  const [imageFile, setImageFile] = useState<File | null>(null);
  const [docxFile, setDocxFile] = useState<File | null>(null);
  const approvalKey = useRef<{ estimate: string; key: string } | null>(null);
  const sceneApprovalKey = useRef<{ estimate: string; key: string } | null>(null);
  const directorKey = useRef<{ fingerprint: string; key: string } | null>(null);
  const dirtyRef = useRef(false);
  const revisionRef = useRef(0);
  const imagePicker = useRef<HTMLInputElement>(null);
  const docxPicker = useRef<HTMLInputElement>(null);
  const base = `/projects/${encodeURIComponent(projectId)}`;

  const load = useCallback(async (preserveLocalEdits = false) => {
    const [saved, currentProject, catalog, sourceInputs, assets, director, generation, jobs] = await Promise.allSettled([
      api<Storyboard>(`${base}/storyboard`), api<Project>(base), api<VideoModel[]>("/models"),
      api<ProjectInput[]>(`${base}/inputs`), api<ReferenceImage[]>(`${base}/assets/reference-images`),
      api<DirectorConfig>("/director-config"), api<GenerationConfig>("/generation-config"),
      api<DirectorJob[]>(`${base}/director/jobs`),
    ]);
    if (saved.status === "rejected") throw saved.reason;
    if (currentProject.status === "rejected") throw currentProject.reason;
    if (catalog.status === "rejected") throw catalog.reason;
    // Polling may finish after someone starts typing. Never replace those edits.
    if (preserveLocalEdits && dirtyRef.current) {
      setConflict(true);
      setMessage("The AI Director saved a new revision. Your local edits are preserved; reload the saved plan when ready.");
      return;
    }
    setBoard(saved.value); setProject(currentProject.value); setModels(catalog.value);
    revisionRef.current = saved.value.revision;
    dirtyRef.current = false; setDirty(false); setConflict(false);
    setValidation(null); setEstimate(null); setSceneEstimate(null); approvalKey.current = null; sceneApprovalKey.current = null;
    const availableInputs = sourceInputs.status === "fulfilled" ? sourceInputs.value : [];
    setInputs(availableInputs);
    const selected = availableInputs.find(input => input.id === saved.value.input_id);
    setSourceText(selected?.text ?? ""); setSourceSaved(selected?.text ?? "");
    setSourceKind(selected?.kind === "prompt" ? "prompt" : "script");
    setSourceSavedKind(selected?.kind === "prompt" ? "prompt" : "script");
    setReferences(assets.status === "fulfilled" ? assets.value : []);
    setDirectorConfig(director.status === "fulfilled" ? director.value : null);
    setGenerationConfig(generation.status === "fulfilled" ? generation.value : null);
    setDirectorJobs(jobs.status === "fulfilled" ? jobs.value : []);
    if (sourceInputs.status === "rejected" || assets.status === "rejected") {
      setMessage("Some input or asset records could not be loaded. Reload them before approving the plan.");
    }
  }, [base]);

  useEffect(() => {
    let active = true;
    load().catch(error => { if (active) setError(errorMessage(error)); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [load]);

  useEffect(() => { dirtyRef.current = dirty || sourceText !== sourceSaved || sourceKind !== sourceSavedKind; }, [dirty, sourceText, sourceSaved, sourceKind, sourceSavedKind]);

  useEffect(() => {
    let active = true;
    const nextRevision = Math.max(0, ...directorJobs.map(job => job.result_revision ?? 0));
    if (nextRevision > revisionRef.current) {
      if (dirtyRef.current) { setConflict(true); setMessage("The AI Director saved a new revision. Your local edits are preserved; reload the saved plan when ready."); }
      else {
        void load(true).then(() => {
          if (!dirtyRef.current) setMessage("The AI storyboard is ready for review. No video clips were submitted.");
        }).catch(error => { if (active) setError(errorMessage(error)); });
      }
    }
    // A fast job can already be terminal in its first snapshot. Reconcile its
    // result above even when there is no longer an active job to poll.
    const timer = directorJobs.some(job => !directorTerminal.has(job.status)) ? setInterval(async () => {
      if (document.visibilityState !== "visible") return;
      try {
        const jobs = await api<DirectorJob[]>(`${base}/director/jobs`);
        if (!active) return;
        setDirectorJobs(jobs);
      } catch (error) { if (active) setError(errorMessage(error)); }
    }, 5000) : null;
    return () => { active = false; if (timer !== null) clearInterval(timer); };
  }, [directorJobs, base, load]);

  useEffect(() => {
    if (!dirty && sourceText === sourceSaved && sourceKind === sourceSavedKind) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty, sourceText, sourceSaved, sourceKind, sourceSavedKind]);

  function invalidate() {
    setValidation(null); setEstimate(null); setSceneEstimate(null); approvalKey.current = null; sceneApprovalKey.current = null; setMessage("");
  }
  function edit(value: Storyboard) {
    setBoard(value); setDirty(true); dirtyRef.current = true; invalidate();
  }
  function report(error: unknown) {
    if (error instanceof ApiError && error.status === 409) {
      if (["revision_conflict", "board_changed_since_estimate", "stale_storyboard"].includes(error.message)) setConflict(true);
      setError(storyboardIssue(error.message));
    } else if (error instanceof ApiError && error.status === 413) {
      setError("This file, script or storyboard is too large. Reduce its size and retry.");
    } else { setError(errorMessage(error)); }
  }
  async function operation(name: string, work: () => Promise<void>) {
    if (busy) return;
    setBusy(name); setError(""); setMessage("");
    try { await work(); } catch (error) { report(error); } finally { setBusy(null); }
  }
  async function saveDraft() {
    if (!board) return;
    await operation("save", async () => {
      const saved = await api<Storyboard>(`${base}/storyboard`, { method: "PUT", body: JSON.stringify(boardPayload(board)) });
      setBoard(saved); revisionRef.current = saved.revision;
      setDirty(false); dirtyRef.current = false; setConflict(false); invalidate();
      setMessage(`Revision ${saved.revision} saved. Existing generated versions remain unchanged.`);
    });
  }
  async function saveInput() {
    await operation("input", async () => {
      await api<ProjectInput>(`${base}/inputs`, json({ kind: sourceKind, text: sourceText, source_name: "Storyboard input" }));
      await load(); setMessage("Script saved. Validate the current scenes against the updated brief before approval.");
    });
  }
  async function importDocx() {
    if (!docxFile) return;
    const invalid = validateUpload(docxFile, "docx"); if (invalid) { setError(invalid); return; }
    await operation("docx", async () => {
      const body = new FormData(); body.append("file", docxFile);
      const input = await api<ProjectInput>(`${base}/imports/docx`, { method: "POST", body });
      await load(); setDocxFile(null);
      if (docxPicker.current) docxPicker.current.value = "";
      setMessage(input.warnings.length ? `Script imported. ${input.warnings.map(storyboardIssue).join(". ")}` : "DOCX text imported. The original archive is not retained.");
    });
  }
  async function uploadReference() {
    if (!imageFile) return;
    const invalid = validateUpload(imageFile, "image"); if (invalid) { setError(invalid); return; }
    await operation("reference", async () => {
      const body = new FormData(); body.append("file", imageFile); body.append("caption", uploadCaption);
      await api<ReferenceImage>(`${base}/assets/reference-images`, { method: "POST", body });
      setReferences(await api<ReferenceImage[]>(`${base}/assets/reference-images`));
      setImageFile(null); setUploadCaption("");
      if (imagePicker.current) imagePicker.current.value = "";
      setMessage("Private reference added. Attach it to a scene and save the draft.");
    });
  }
  async function validate() {
    if (!board) return;
    await operation("validate", async () => {
      const result = await api<StoryboardValidation>(`${base}/storyboard/validate`, json({ expected_revision: board.revision }));
      setValidation(result); setEstimate(null);
      if (result.valid) setMessage(`Storyboard timing and scene settings are valid within ${result.tolerance_seconds}s tolerance.`);
    });
  }
  async function estimatePlan() {
    if (!board) return;
    await operation("estimate", async () => {
      const quote = await api<StoryboardEstimate>(`${base}/storyboard/estimate`, json({ expected_revision: board.revision, retry_allowance_percent: retryAllowance }));
      setEstimate(quote); approvalKey.current = { estimate: quote.id, key: crypto.randomUUID() };
    });
  }
  async function approvePlan(cap: string, approvePrice: boolean) {
    if (!board || !estimate || dirty || sourceText !== sourceSaved || sourceKind !== sourceSavedKind || !generationConfig?.paid_generation_enabled) return;
    if (!approvalKey.current || approvalKey.current.estimate !== estimate.id) approvalKey.current = { estimate: estimate.id, key: crypto.randomUUID() };
    await operation("approve", async () => {
      await api<{ jobs: GenerationJob[] }>(`${base}/storyboard/approve`, json({ expected_revision: board.revision, estimate_id: estimate.id, idempotency_key: approvalKey.current!.key, approved_cap_usd: cap, approve_unverified_price: approvePrice }));
      await load(); setMessage("The reviewed scene jobs were approved atomically. Track their progress in project details.");
    });
  }
  async function estimateScene(scene: StoryboardScene) {
    if (!scene.id || dirty || sourceText !== sourceSaved || sourceKind !== sourceSavedKind) return;
    await operation("scene-estimate", async () => {
      const quote = await api<Estimate>(`${base}/generations/estimate`, json({ ...sceneSettings(scene), prompt: scene.visual_prompt, negative_prompt: scene.negative_prompt, scene_id: scene.id, retry_allowance_percent: retryAllowance }));
      setSceneEstimate({ quote, sceneKey: scene.scene_id }); setEstimate(null);
      sceneApprovalKey.current = { estimate: quote.id, key: crypto.randomUUID() };
    });
  }
  async function approveScene(cap: string, approvePrice: boolean) {
    if (!sceneEstimate || dirty || sourceText !== sourceSaved || sourceKind !== sourceSavedKind || !generationConfig?.paid_generation_enabled) return;
    if (!sceneApprovalKey.current || sceneApprovalKey.current.estimate !== sceneEstimate.quote.id) sceneApprovalKey.current = { estimate: sceneEstimate.quote.id, key: crypto.randomUUID() };
    await operation("scene-approve", async () => {
      await api<GenerationJob>(`${base}/generations`, json({ estimate_id: sceneEstimate.quote.id, idempotency_key: sceneApprovalKey.current!.key, approved_cap_usd: cap, approve_unverified_price: approvePrice }));
      await load(); setMessage("Only the selected scene generation was approved. Other scene versions remain unchanged.");
    });
  }
  async function planWithAi() {
    if (!board?.input_id || dirty || sourceText !== sourceSaved || sourceKind !== sourceSavedKind || !directorConfig?.director_enabled || !directorConfig.provider_configured || !directorApproved) return;
    const fingerprint = `${board.input_id}:${board.revision}:${directorCap}`;
    if (directorKey.current?.fingerprint !== fingerprint) directorKey.current = { fingerprint, key: crypto.randomUUID() };
    await operation("director", async () => {
      await api<DirectorJob>(`${base}/director/jobs`, json({ input_id: board.input_id, expected_revision: board.revision, idempotency_key: directorKey.current!.key, max_cost_usd: directorCap, approve_unknown_price: true }));
      setDirectorJobs(await api<DirectorJob[]>(`${base}/director/jobs`));
      setProject(await api<Project>(base)); setDirectorApproved(false);
      setMessage("Director job approved. Its result will become a new storyboard revision for review; it will not submit video clips.");
    });
  }

  if (loading) return <div className="loading-panel" role="status"><span className="spinner"/>Loading your production plan…</div>;
  if (!board || !project) return <Notice kind="error">{error || "Storyboard not found."}</Notice>;
  const inputDirty = sourceText !== sourceSaved || sourceKind !== sourceSavedKind;
  const dirtyPlan = dirty || inputDirty;
  const activeDirector = directorJobs.some(job => !directorTerminal.has(job.status));
  const unresolvedDirector = directorJobs.some(job => job.status === "reconciliation_required");
  const directorUnavailable = !directorConfig?.director_enabled || !directorConfig.provider_configured;
  const directorCapNumber = Number(directorCap);
  const directorCapValid = Number.isFinite(directorCapNumber) && directorCapNumber > 0 && Math.round(directorCapNumber * 1e6) <= Math.round((Number(project.budget_usd) - Number(project.committed_cost_usd)) * 1e6);
  const approveBlocked = dirtyPlan || conflict || !validation?.valid || validation.revision !== board.revision || estimate?.revision !== board.revision;

  return <>
    {project.archived && <Notice>Archived projects are read-only. Restore this project from the <Link className="text-link" href="/library">project library</Link> before changing its storyboard.</Notice>}
    <div className="breadcrumb"><Link href="/">Workspace</Link><span>/</span><Link href={`/projects/${projectId}`}>{project.name}</Link><span>/</span><span>Storyboard</span></div>
    <div className="page-heading"><div><span className="eyebrow">AI DIRECTOR & SCENE WORKSPACE</span><h1>Your story, scene by scene</h1><p>Review every frame and word before approving generation.</p></div><span className="subtle-badge">REVISION {board.revision} · {board.status.replaceAll("_", " ")}</span></div>
    {error && <Notice kind="error">{error}</Notice>}{message && <Notice>{message}</Notice>}
    {conflict && <Notice kind="error"><strong>The saved plan changed.</strong> Your local edits have not been overwritten. Review the latest saved revision before trying again.</Notice>}
    {dirtyPlan && <Notice><strong>Unsaved changes.</strong> Save the draft and input before validating, requesting AI work or approving spending.</Notice>}
    <div className="storyboard-toolbar"><div><strong>{dirty ? "Draft has local changes" : `Saved revision ${board.revision}`}</strong><span>Scene edits preserve previously generated versions.</span></div><div className="toolbar-actions"><button type="button" className="button button-secondary button-small" disabled={Boolean(busy)} onClick={() => operation("reload", load)}>{dirtyPlan ? "Discard local changes & reload" : "Reload saved plan"}</button>{canEdit && <button type="button" className="button button-primary button-small" disabled={!dirty || Boolean(busy) || conflict} onClick={saveDraft}>{busy === "save" ? "Saving…" : "Save draft"}</button>}</div></div>
    <div className="storyboard-layout"><div className="storyboard-main">
      <StoryboardEditor board={board} models={models} references={references} disabled={!canEdit || Boolean(busy)} onChange={edit} onEstimateScene={estimateScene} sceneActionsDisabled={dirtyPlan || conflict}/>
      <section className="panel storyboard-review"><div className="panel-heading"><span className="step-number">02</span><div><h2>Validate & cost the plan</h2><p>Check duration, supported settings and references before spending.</p></div></div>
        <div className="review-actions"><button type="button" className="button button-secondary" disabled={dirtyPlan || conflict || Boolean(busy) || !canEdit} onClick={validate}>{busy === "validate" ? "Validating…" : "Validate saved storyboard"}</button><label>Retry allowance (%)<input type="number" min={0} max={100} step={1} value={retryAllowance} disabled={Boolean(busy) || !canEdit} onChange={event => { setRetryAllowance(Number(event.target.value)); setEstimate(null); setSceneEstimate(null); }}/></label><button type="button" className="button button-primary" disabled={dirtyPlan || conflict || !validation?.valid || validation.revision !== board.revision || Boolean(busy) || !canEdit} onClick={estimatePlan}>{busy === "estimate" ? "Estimating…" : "Estimate all scenes"}</button></div>
        {validation && <div className={`validation-result ${validation.valid ? "validation-valid" : "validation-invalid"}`} role={validation.valid ? "status" : "alert"}><strong>{validation.valid ? "Plan validated" : "This plan needs attention"}</strong><p>{validation.total_duration_seconds}s of {validation.target_duration_seconds}s target · tolerance {validation.tolerance_seconds}s</p>{!!validation.issues.length && <ul>{validation.issues.map((issue, index) => <li key={`${issue.scene_id}:${issue.code}:${index}`}>{issue.scene_id ? `${issue.scene_id}: ` : ""}{storyboardIssue(issue.code)}</li>)}</ul>}</div>}
        {estimate && <div className="plan-cost-table"><div><strong>Scene estimates</strong><span>Quote bound to revision {estimate.revision}</span></div><table><thead><tr><th>Scene</th><th>Estimate</th><th>Pricing</th></tr></thead><tbody>{estimate.scenes.map(scene => <tr key={scene.scene_id}><td>{scene.scene_key}</td><td>{money(scene.total_usd)}</td><td>{scene.pricing_verified ? "Verified" : "Unverified / unavailable"}</td></tr>)}</tbody></table></div>}
      </section>
      {estimate && <ApprovalPanel key={`${estimate.id}:${board.revision}`} scope="storyboard" estimate={{ id: estimate.id, base_usd: estimate.base_usd, total_usd: estimate.total_usd, pricing_verified: estimate.pricing_verified, source: "storyboard_plan", expires_at: estimate.expires_at, request: {} }} config={generationConfig} project={project} busy={Boolean(busy)} blocked={approveBlocked || !canEdit} onApprove={approvePlan}/>}
      {sceneEstimate && <section aria-label="Individual scene approval"><h2 className="scene-estimate-title">Generate only {sceneEstimate.sceneKey}</h2><ApprovalPanel key={sceneEstimate.quote.id} estimate={sceneEstimate.quote} config={generationConfig} project={project} busy={Boolean(busy)} blocked={dirtyPlan || conflict || !canEdit} onApprove={approveScene}/></section>}
    </div><aside className="storyboard-sidebar">
      <section className="panel source-panel"><h2>Source script</h2><p className="field-help">The saved input is versioned separately from the scene draft.</p><fieldset className="request-fields" disabled={!canEdit || Boolean(busy)}><label>Text input type<select value={sourceKind} onChange={event => { setSourceKind(event.target.value as "prompt" | "script"); setDirectorApproved(false); }}><option value="prompt">Text prompt</option><option value="script">Full script</option></select></label><label>Script or prompt<textarea value={sourceText} rows={8} maxLength={100000} onChange={event => { setSourceText(event.target.value); setDirectorApproved(false); }}/></label><button type="button" className="button button-secondary button-small" disabled={dirty || !sourceText.trim() || !inputDirty} onClick={saveInput}>Save input</button><label className="upload-label">Replace with DOCX<input ref={docxPicker} type="file" accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document" onChange={event => { const file = event.target.files?.[0]; const invalid = file ? validateUpload(file, "docx") : null; if (invalid) { setError(invalid); setDocxFile(null); event.target.value = ""; } else { setDocxFile(file ?? null); setError(""); } }}/></label><button type="button" className="button button-secondary button-small" disabled={dirty || !docxFile} onClick={importDocx}>Import DOCX text</button></fieldset><p className="field-help">Save scene changes before replacing the input. Max 5 MiB DOCX; only extracted text is retained.</p>{inputs.find(input => input.id === board.input_id)?.source_name && <span className="source-name">Source: {inputs.find(input => input.id === board.input_id)?.source_name}</span>}</section>
      <section className="panel director-panel"><span className="eyebrow">AI DIRECTOR</span><h2>Draft a production plan</h2><p>Use the saved script and production goals to propose scenes. You review the result before video generation.</p>{directorUnavailable && <Notice><strong>AI planning is disabled.</strong> An operator must configure and approve the OpenAI integration. You can edit scenes manually now.</Notice>}<fieldset className="request-fields" disabled={!canEdit || Boolean(busy) || directorUnavailable}><label>Maximum AI planning reservation (USD)<input type="number" min="0.000001" step="0.000001" max={project.budget_usd} value={directorCap} onChange={event => { setDirectorCap(event.target.value); setDirectorApproved(false); }}/></label><label className="checkbox-row"><input type="checkbox" checked={directorApproved} onChange={event => setDirectorApproved(event.target.checked)}/><span>I approve reserving this amount from the project budget for one AI planning request. Token pricing and final charges are unverified.</span></label><button type="button" className="button button-primary full-width" disabled={dirtyPlan || conflict || !board.input_id || !directorApproved || !directorCapValid || activeDirector || unresolvedDirector} onClick={planWithAi}>{busy === "director" ? "Preparing…" : "Generate AI storyboard"}</button></fieldset><p className="field-help">Reference captions, dimensions and IDs are planning context; image pixels are not sent in this phase. Failed or interrupted AI requests are never retried automatically.</p>{directorJobs.map(job => <div key={job.id} className="director-job"><StatusPill status={job.status}/><span>Reserved {money(job.reserved_cost_usd)} · actual {job.actual_cost_usd === null ? "pending" : money(job.actual_cost_usd)}</span>{job.error_code && <p role="alert">{storyboardIssue(job.error_code)}</p>}</div>)}</section>
      <section className="panel reference-library"><h2>Planning references</h2><p className="field-help">Private PNG, JPEG or WebP images, 5 MiB maximum; static images only, minimum 300px, maximum 8 megapixels.</p><fieldset className="request-fields" disabled={!canEdit || Boolean(busy)}><label>Reference image<input ref={imagePicker} type="file" accept="image/png,image/jpeg,image/webp" onChange={event => { const file = event.target.files?.[0]; const invalid = file ? validateUpload(file, "image") : null; if (invalid) { setError(invalid); setImageFile(null); event.target.value = ""; } else { setImageFile(file ?? null); setError(""); } }}/></label><label>Reference caption<input value={uploadCaption} maxLength={500} onChange={event => setUploadCaption(event.target.value)} placeholder="Describe the visual reference"/></label><button type="button" className="button button-secondary button-small" disabled={!imageFile} onClick={uploadReference}>{busy === "reference" ? "Uploading…" : "Upload private reference"}</button></fieldset><div className="reference-library-grid">{references.map(reference => <figure key={reference.id}><img src={`/api/v1/assets/${encodeURIComponent(reference.id)}/preview`} loading="lazy" alt={reference.metadata_json.caption || reference.original_filename || "Planning reference"}/><figcaption><strong>{reference.metadata_json.caption || reference.original_filename || "Reference image"}</strong><span>{reference.metadata_json.width} × {reference.metadata_json.height}</span></figcaption></figure>)}</div><p className="field-help">Attach or replace these images in individual scene cards. Image-to-video submission remains disabled until provider delivery is configured.</p></section>
      <Link className="button button-primary" href={`/projects/${projectId}/production`}><Icon name="play" size={16}/>Produce complete video</Link>
      <Link className="text-link" href={`/projects/${projectId}`}>Open clip generations<Icon name="arrow" size={15}/></Link>
    </aside></div>
  </>;
}
