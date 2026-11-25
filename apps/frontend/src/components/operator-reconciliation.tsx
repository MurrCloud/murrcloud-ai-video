"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api, errorMessage, money } from "@/lib/api";
import { localTime, validEvidenceReference, validProviderRequestId, validVerifiedAmount, workspaceIssue } from "@/lib/workspace";
import type { PageResult, ReconciliationJob, UnconfirmedCost } from "@/lib/workspace";
import { useUser } from "./studio-shell";
import { Notice, StatusPill } from "./ui";
import { LoadingPanel, Pagination, useActionLock, useActionPanelFocus, useMutationKey } from "./workspace-common";

type Target = { job: ReconciliationJob; type: "job" } | { cost: UnconfirmedCost; type: "cost" };
export function OperatorReconciliation({ refresh, onChange }: { refresh: number; onChange: () => void }) {
  const admin = useUser()?.role === "admin";
  const [kind, setKind] = useState("generation"); const [jobOffset, setJobOffset] = useState(0); const [costOffset, setCostOffset] = useState(0);
  const [jobs, setJobs] = useState<PageResult<ReconciliationJob> | null>(null); const [costs, setCosts] = useState<PageResult<UnconfirmedCost> | null>(null);
  const [loading, setLoading] = useState(true); const [listErrors, setListErrors] = useState<string[]>([]); const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [message, setMessage] = useState("");
  const [target, setTarget] = useState<Target | null>(null); const [outcome, setOutcome] = useState(""); const [providerId, setProviderId] = useState(""); const [amount, setAmount] = useState(""); const [chargeId, setChargeId] = useState(""); const [reference, setReference] = useState(""); const [ack, setAck] = useState(false); const [conflict, setConflict] = useState(false); const key = useMutationKey();
  const actionLock = useActionLock(); const actionPanel = useActionPanelFocus(target);
  const limit = 25;
  useEffect(() => {
    if (!admin) { setLoading(false); return; }
    let active = true; setLoading(true); setListErrors([]);
    Promise.allSettled([
      api<PageResult<ReconciliationJob>>(`/operations/reconciliations?${new URLSearchParams({ kind, limit: String(limit), offset: String(jobOffset) })}`),
      api<PageResult<UnconfirmedCost>>(`/operations/costs?${new URLSearchParams({ limit: String(limit), offset: String(costOffset) })}`),
    ]).then(([outcomes, billing]) => {
      if (!active) return; const errors: string[] = [];
      if (outcomes.status === "fulfilled") { setJobs(outcomes.value); if (jobOffset > 0 && jobOffset >= outcomes.value.total) setJobOffset(Math.floor(Math.max(0, outcomes.value.total - 1) / limit) * limit); }
      else { setJobs(null); errors.push(`Provider outcomes: ${errorMessage(outcomes.reason)}`); }
      if (billing.status === "fulfilled") { setCosts(billing.value); if (costOffset > 0 && costOffset >= billing.value.total) setCostOffset(Math.floor(Math.max(0, billing.value.total - 1) / limit) * limit); }
      else { setCosts(null); errors.push(`Unconfirmed charges: ${errorMessage(billing.reason)}`); }
      setListErrors(errors);
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [admin, kind, jobOffset, costOffset, refresh]);
  function open(value: Target) { setTarget(value); setOutcome(""); setProviderId(value.type === "job" ? value.job.provider_job_id ?? "" : ""); setAmount(""); setChargeId(""); setReference(""); setAck(false); setConflict(false); setError(""); setMessage(""); key.clear(); }
  const valid = ack && validEvidenceReference(reference) && !conflict && (target?.type === "job" ? outcome === "verified_failed" || (outcome === "attach_provider_id" && target.job.kind === "generation" && validProviderRequestId(providerId)) : target?.type === "cost" && validVerifiedAmount(amount) && (!chargeId || /^[A-Za-z0-9_.:-]{1,255}$/.test(chargeId)));
  async function submit(event: React.FormEvent) {
    event.preventDefault(); if (!target || !admin || !valid || busy || !actionLock.acquire()) return; setBusy(true); setError("");
    const common = { reference: reference.trim(), acknowledge_verified: true };
    try {
      if (target.type === "job") {
        const payload = { ...common, expected_updated_at: target.job.updated_at, outcome, ...(outcome === "attach_provider_id" ? { provider_job_id: providerId.trim() } : {}) };
        await api(`/operations/reconciliations/${target.job.kind}/${target.job.id}`, { method: "POST", body: JSON.stringify({ ...payload, idempotency_key: key.get({ target: target.job.id, ...payload }) }) });
        setMessage(outcome === "attach_provider_id" ? "Existing provider request attached for polling. No new paid generation was submitted." : "Verified provider failure recorded. Reservations and unknown charges remain unchanged.");
      } else {
        const payload = { ...common, expected_updated_at: target.cost.updated_at, amount_usd: amount.trim(), ...(chargeId ? { provider_charge_id: chargeId } : {}) };
        await api(`/operations/costs/${target.cost.id}/confirm`, { method: "POST", body: JSON.stringify({ ...payload, idempotency_key: key.get({ target: target.cost.id, ...payload }) }) });
        setMessage("Verified provider charge recorded. Project reservations were not released.");
      }
      setTarget(null); key.clear(); onChange();
    } catch (error) { const code = errorMessage(error); setError(workspaceIssue(code)); if (["revision_conflict", "reconciliation_not_required", "charge_already_confirmed"].includes(code)) setConflict(true); }
    finally { actionLock.release(); setBusy(false); }
  }
  if (!admin) return null;
  return <>
    <section className="panel operator-intro"><h2>Provider and billing reconciliation</h2><p>Use a verified provider statement, invoice or support record. A failed job may still incur charges. Recording evidence does not release reservations or authorize another generation.</p></section>
    {listErrors.map(value => <Notice kind="error" key={value}>{value}</Notice>)}{error && <Notice kind="error">{error}</Notice>}{message && <Notice kind="success">{message}</Notice>}
    {target && <section className="panel workspace-action" aria-label={target.type === "job" ? "Provider reconciliation" : "Charge confirmation"} ref={actionPanel}><div className="section-heading"><h2>{target.type === "job" ? "Review provider outcome" : "Review final provider charge"}</h2><button className="button button-secondary button-small" disabled={busy} onClick={() => setTarget(null)}>Close review</button></div><p className="field-help">{target.type === "job" ? `${target.job.kind} job ${target.job.id}` : `Cost record ${target.cost.id}`} · Project {target.type === "job" ? target.job.project_id : target.cost.project_id}</p><form onSubmit={submit}><fieldset disabled={busy || conflict}>
      {target.type === "job" ? <><label>Verified provider outcome<select value={outcome} onChange={e => { setOutcome(e.target.value); setAck(false); }}><option value="">Choose the verified outcome</option><option value="verified_failed">Provider confirms failure</option>{target.job.kind === "generation" && <option value="attach_provider_id">Attach an existing request ID</option>}</select></label>{outcome === "attach_provider_id" && <label>Existing provider request UUID<input value={providerId} onChange={e => { setProviderId(e.target.value); setAck(false); }} maxLength={36}/><span className="field-help">Use the exact request ID verified with the provider. This resumes polling the existing request.</span></label>}</> : <><label>Verified final charge (USD)<input value={amount} onChange={e => { setAmount(e.target.value); setAck(false); }} inputMode="decimal"/><span className="field-help">Enter the final amount explicitly, including zero if verified. Up to $1,000,000 with six decimal places.</span></label><label>Provider charge ID (optional)<input value={chargeId} onChange={e => { setChargeId(e.target.value); setAck(false); }} maxLength={255}/></label><p className="field-help">Resolve an uncertain or active provider job before confirming its final charge.</p></>}
      <label>Provider statement or support reference<input value={reference} onChange={e => { setReference(e.target.value); setAck(false); }} minLength={3} maxLength={240}/><span className="field-help">Use an invoice/support reference. Do not enter API keys, passwords or customer content.</span></label>
      <label className="checkbox-row"><input type="checkbox" checked={ack} onChange={e => setAck(e.target.checked)}/><span>{target.type === "job" ? "I checked the provider record and verified this exact job and outcome." : "I checked the provider invoice or statement and verified this exact final charge."}</span></label>
      <button className="button button-primary" disabled={!valid}>{busy ? "Recording…" : target.type === "cost" ? "Record verified charge" : outcome === "attach_provider_id" ? "Attach ID & resume polling" : "Record verified failure"}</button>
    </fieldset></form>{conflict && <p className="field-help">The saved record changed. Close this form and refresh status before reviewing it again.</p>}</section>}
    {loading ? <LoadingPanel>Loading provider and billing records…</LoadingPanel> : <><section className="panel"><div className="section-heading"><h2>Uncertain provider outcomes</h2><label>Uncertain job type<select value={kind} disabled={busy} onChange={e => { setKind(e.target.value); setJobOffset(0); }}><option value="generation">Video generation</option><option value="director">AI director</option></select></label></div>{jobs && (jobs.items.length ? <><div className="workspace-table"><table><thead><tr><th scope="col">Provider / model</th><th scope="col">Project</th><th scope="col">Reservation</th><th scope="col">Provider request</th><th scope="col">Updated</th><th scope="col">Review</th></tr></thead><tbody>{jobs.items.map(job => <tr key={job.id}><th scope="row">{job.provider}<span className="table-detail">{job.model_id}</span></th><td><Link className="text-link" href={`/projects/${job.project_id}`}>{job.project_id.slice(0, 8)}</Link></td><td>{money(job.reserved_usd)}</td><td>{job.provider_job_id || "Not recorded"}</td><td>{localTime(job.updated_at)}</td><td><button className="button button-secondary button-small" disabled={busy} onClick={() => open({ type: "job", job })}>Review outcome</button></td></tr>)}</tbody></table></div><Pagination offset={jobs.offset} count={jobs.items.length} total={jobs.total} limit={jobs.limit} busy={busy} onPage={setJobOffset}/></> : <p className="field-help">No uncertain {kind} jobs.</p>)}</section>
    <section className="panel"><h2>Unconfirmed provider charges</h2><p className="field-help">Pending and unavailable billing records require verified final evidence.</p>{costs && (costs.items.length ? <><div className="workspace-table"><table><thead><tr><th scope="col">Provider / model</th><th scope="col">Project</th><th scope="col">Status</th><th scope="col">Recorded amount</th><th scope="col">Updated</th><th scope="col">Review</th></tr></thead><tbody>{costs.items.map(cost => <tr key={cost.id}><th scope="row">{cost.provider}<span className="table-detail">{cost.model_id || "Model not recorded"}</span></th><td><Link className="text-link" href={`/projects/${cost.project_id}`}>{cost.project_id.slice(0, 8)}</Link></td><td><StatusPill status={cost.status}/></td><td>{money(cost.amount_usd)}</td><td>{localTime(cost.updated_at)}</td><td><button className="button button-secondary button-small" disabled={busy} onClick={() => open({ type: "cost", cost })}>Review charge</button></td></tr>)}</tbody></table></div><Pagination offset={costs.offset} count={costs.items.length} total={costs.total} limit={costs.limit} busy={busy} onPage={setCostOffset}/></> : <p className="field-help">No unconfirmed provider charges.</p>)}</section></>}
  </>;
}
