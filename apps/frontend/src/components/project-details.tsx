"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, errorMessage, money } from "@/lib/api";
import type { UsageReport, UsageTotals } from "@/lib/workspace";
import type { GenerationConfig, GenerationJob, Project, VideoModel } from "@/lib/types";
import { useUser } from "./studio-shell";
import { EmptyState, Icon, Notice, StatusPill } from "./ui";

const terminal = new Set(["succeeded", "failed", "cancelled", "abandoned", "reconciliation_required"]);
const downloadPath = (job: GenerationJob) => job.output_asset_id ? `/api/v1/assets/${encodeURIComponent(job.output_asset_id)}/download` : null;

export function ProjectDetails({ id }: { id: string }) {
  const user = useUser();
  const [project, setProject] = useState<Project | null>(null);
  const [jobs, setJobs] = useState<GenerationJob[]>([]);
  const [models, setModels] = useState<VideoModel[]>([]);
  const [config, setConfig] = useState<GenerationConfig | null>(null);
  const [error, setError] = useState("");
  const [financials, setFinancials] = useState<UsageTotals | null>(null);
  const [financialError, setFinancialError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const canEdit = (user?.role === "admin" || user?.role === "editor") && !project?.archived;
  useEffect(() => {
    let active = true; setFinancials(null); setFinancialError(false);
    Promise.allSettled([api<Project>(`/projects/${encodeURIComponent(id)}`), api<GenerationJob[]>(`/projects/${encodeURIComponent(id)}/generations`), api<VideoModel[]>("/models"), api<GenerationConfig>("/generation-config"), api<UsageReport>(`/reports/usage?${new URLSearchParams({ project_id: id, limit: "1", offset: "0" })}`)]).then(([p, j, m, c, report]) => {
      if (!active) return;
      setFinancials(report.status === "fulfilled" ? report.value.totals : null); setFinancialError(report.status === "rejected");
      if (p.status === "rejected" || j.status === "rejected") { setError(errorMessage(p.status === "rejected" ? p.reason : j.status === "rejected" ? j.reason : null)); return; }
      setProject(p.value); setJobs(j.value); setError("");
      setModels(m.status === "fulfilled" ? m.value : []);
      setConfig(c.status === "fulfilled" ? c.value : null);
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [id, refresh]);
  useEffect(() => {
    if (!jobs.some(job => !terminal.has(job.status))) return;
    const timer = setInterval(() => { if (document.visibilityState === "visible") setRefresh(value => value + 1); }, 5000);
    return () => clearInterval(timer);
  }, [jobs]);
  async function action(job: GenerationJob, operation: "cancel" | "dispatch") {
    if (busy) return;
    setBusy(job.id); setError("");
    try { await api(`/projects/${id}/generations/${job.id}/${operation}`, { method: "POST" }); setRefresh(value => value + 1); }
    catch (error) { setError(errorMessage(error)); }
    finally { setBusy(null); }
  }
  if (loading) return <div className="loading-panel" role="status"><span className="spinner"/>Loading project…</div>;
  if (!project) return <Notice kind="error">{error || "Project not found."}</Notice>;
  return <><div className="breadcrumb"><Link href="/">Workspace</Link><span>/</span><span>Project details</span></div><div className="page-heading"><div><span className="eyebrow">PRODUCTION CONTROL</span><h1>{project.name}</h1><p>Track each clip, version and committed cost.</p></div><div className="toolbar-actions"><Link href={`/projects/${id}/production`} className="button button-secondary"><Icon name="play" size={17}/>Produce video</Link><Link href={`/projects/${id}/storyboard`} className="button button-secondary"><Icon name="film" size={17}/>Storyboard</Link>{canEdit && <Link href={`/projects/new?project=${id}`} className="button button-primary"><Icon name="plus" size={17}/>Add a clip</Link>}</div></div>{error && <Notice kind="error">{error}</Notice>}
    <section className="project-overview panel"><div><StatusPill status={project.status}/><h2>Project brief</h2><p className="project-prompt">{project.prompt || "No brief added."}</p></div><dl><div><dt>Configured budget</dt><dd>{money(project.budget_usd)}</dd></div><div><dt>Committed / reserved</dt><dd>{money(project.committed_cost_usd)}</dd></div><div><dt>Spending exposure</dt><dd>{money(financials?.exposure_usd)}</dd></div><div><dt>Budget remaining</dt><dd className={financials && Number(financials.remaining_budget_usd) < 0 ? "financial-negative" : undefined}>{money(financials?.remaining_budget_usd)}</dd></div><div><dt>Authorization remaining</dt><dd className={financials && Number(financials.remaining_approved_usd) < 0 ? "financial-negative" : undefined}>{money(financials?.remaining_approved_usd)}</dd></div><div><dt>Approved spending cap</dt><dd>{project.approved_cap_usd === null ? "Not approved" : money(project.approved_cap_usd)}</dd></div></dl></section>
    {financialError && <Notice kind="error">Current spending report could not be loaded. Exposure and remaining balances are unavailable. Refresh to retry; reservations alone do not determine the remaining balance.</Notice>}
    {financials && (Number(financials.remaining_budget_usd) < 0 || Number(financials.remaining_approved_usd) < 0) && <Notice kind="error">Spending exposure exceeds this project's configured budget or approved cap. Negative remaining balances show the deficit. Review charges and approvals before further work.</Notice>}
    <p className="small muted">Exposure includes held reservations, verified charge overruns and standalone charges. A configured budget does not authorize spending; the approved cap is enforced separately.</p>
    {project.archived && <Notice>This project is archived. Restore it from the <Link href="/library">project library</Link> before starting new work. Existing downloads remain available.</Notice>}
    {!config?.paid_generation_enabled && <Notice>Paid generation is disabled. Existing job history and available downloads remain accessible.</Notice>}
    <div className="section-heading"><div><h2>Clip generations</h2><p>Each regeneration retains the earlier scene version.</p></div><button className="button button-secondary button-small" onClick={() => setRefresh(value => value + 1)}><Icon name="refresh" size={15}/>Refresh</button></div>
    {jobs.length ? <div className="job-list">{jobs.map((job, index) => {
      const path = downloadPath(job);
      const model = models.find(m => m.model_id === job.model_id);
      const complete = job.status === "succeeded";
      return <article className="panel job-card" key={job.id}><div className={`job-thumbnail ${complete ? "job-thumbnail-ready" : ""}`}><Icon name={complete ? "check" : "film"} size={30}/><span>CLIP {String(index + 1).padStart(2, "0")}</span></div><div className="job-content"><div className="job-title"><h3>{model?.display_name ?? job.model_id}</h3><StatusPill status={job.status}/></div><p className="small muted">Scene {job.scene_id?.slice(0, 8)} · Version {job.scene_version_id?.slice(0, 8)}</p><div className="job-costs"><span>Estimate <strong>{money(job.estimated_cost_usd)}</strong></span><span>Reserved <strong>{money(job.reserved_cost_usd)}</strong></span><span>Actual charges <strong>{job.actual_cost_usd === null ? "Pending" : money(job.actual_cost_usd)}</strong></span></div>{job.last_error_code && <p className="job-error" role="alert">{job.status === "reconciliation_required" ? "Submission outcome is uncertain. An operator must reconcile it before any paid retry." : `Generation needs attention: ${job.last_error_code.replaceAll("_", " ")}.`}</p>}{job.no_longer_wanted && <p className="small muted">Cancellation requested locally. Provider charges may still apply.</p>}<div className="job-actions">{complete && path && <a href={path} className="button button-primary button-small"><Icon name="download" size={15}/>Download MP4</a>}{canEdit && terminal.has(job.status) && job.status !== "reconciliation_required" && <Link href={`/projects/new?project=${id}&scene=${job.scene_id}&model=${encodeURIComponent(job.model_id)}`} className="button button-secondary button-small"><Icon name="refresh" size={15}/>Regenerate scene</Link>}{canEdit && !terminal.has(job.status) && !job.no_longer_wanted && <button className="button button-secondary button-small" disabled={busy === job.id} onClick={() => action(job, "cancel")}>{busy === job.id ? "Updating…" : "Cancel generation"}</button>}{canEdit && job.status === "approved" && config?.paid_generation_enabled && <button className="button button-secondary button-small" disabled={busy === job.id} onClick={() => action(job, "dispatch")}>Resume dispatch</button>}</div></div></article>;
    })}</div> : <div className="panel"><EmptyState title="Ready for your first clip" action={canEdit ? <Link href={`/projects/new?project=${id}`} className="button button-primary"><Icon name="plus" size={17}/>Configure a clip</Link> : undefined}>Choose a model and review the estimate to prepare this project's first generation.</EmptyState></div>}
    <p className="small muted section-note">Reserved amounts protect the approved cap. They are not confirmed provider charges. Cancelling or failing a paid job does not automatically release the reservation.</p>
  </>;
}
