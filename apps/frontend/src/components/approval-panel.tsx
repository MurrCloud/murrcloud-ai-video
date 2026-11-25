"use client";

import { useEffect, useState } from "react";
import { money } from "@/lib/api";
import { generationBlockReason } from "@/lib/model-settings";
import type { Estimate, GenerationConfig, Project } from "@/lib/types";
import { Icon, Notice } from "./ui";

export function ApprovalPanel({ estimate, config, project, busy, onApprove, scope = "clip", blocked = false }: { estimate: Estimate; config: GenerationConfig | null; project: Project; busy: boolean; onApprove: (cap: string, approvePrice: boolean) => Promise<void> | void; scope?: "clip" | "storyboard"; blocked?: boolean }) {
  const [cap, setCap] = useState(project.budget_usd);
  const [budgetApproved, setBudgetApproved] = useState(false);
  const [priceApproved, setPriceApproved] = useState(false);
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);
  const unavailable = estimate.total_usd === null;
  const unverified = !estimate.pricing_verified || unavailable;
  const reason = generationBlockReason(config, estimate, budgetApproved, priceApproved, cap, project.budget_usd, project.committed_cost_usd, now);
  async function approve() {
    if (busy || blocked || generationBlockReason(config, estimate, budgetApproved, priceApproved, cap, project.budget_usd, project.committed_cost_usd)) return;
    await onApprove(cap, priceApproved);
  }
  return <section className="panel approval-panel" aria-label="Generation approval"><div className="panel-heading"><span className="step-number">03</span><div><h2>Review & approve</h2><p>Your project spending cap is enforced before submission.</p></div></div><div className="estimate-cost"><span>{scope === "storyboard" ? "Estimated storyboard total" : "Estimated clip total"} <small>Includes the configured retry allowance</small></span><strong>{money(estimate.total_usd)}</strong></div><div className="estimate-meta"><span>Base generation</span><span>{money(estimate.base_usd)}</span><span>Pricing source</span><span>{estimate.source.replaceAll("_", " ")}</span><span>Estimate valid until</span><span>{new Date(estimate.expires_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span></div>
    {unverified && <Notice>{unavailable ? "Pricing is unavailable." : "Public pricing has not been verified for your billing account."} The full remaining approved project cap will be reserved. Provider charges may remain pending after completion or cancellation, which can block further jobs until billing is reconciled.</Notice>}
    <label>Project spending approval cap (USD)<input type="number" min="0.0001" max={project.budget_usd} step="0.0001" value={cap} onChange={e => { setCap(e.target.value); setBudgetApproved(false); }}/><span className="field-help">Total project cap, including existing commitments of {money(project.committed_cost_usd)}. Project budget: {money(project.budget_usd)}.</span></label>
    <label className="checkbox-row"><input type="checkbox" checked={budgetApproved} onChange={e => setBudgetApproved(e.target.checked)}/><span>I approve this total project spending cap and the reservation for this generation.</span></label>
    {unverified && <label className="checkbox-row"><input type="checkbox" checked={priceApproved} onChange={e => setPriceApproved(e.target.checked)}/><span>I acknowledge {unavailable ? "unavailable" : "unverified"} pricing and approve reserving the remaining cap. Actual provider charges are not confirmed.</span></label>}
    {!config?.paid_generation_enabled && <Notice><strong>Paid generation is disabled.</strong> An operator must approve and enable it before any provider submission.</Notice>}
    {reason && config?.paid_generation_enabled && <p className="approval-reason" role="status">{reason}</p>}
    <button type="button" className="button button-primary full-width" disabled={Boolean(reason) || busy || blocked} onClick={approve}>{busy ? "Preparing generation…" : <><Icon name="play" size={17}/>{config?.paid_generation_enabled ? scope === "storyboard" ? "Approve & generate all scenes" : "Approve & generate clip" : "Generation disabled"}</>}</button>
    <p className="small muted approval-footnote">No provider request is submitted while generation is disabled. An approved retry allowance does not trigger automatic paid retries.</p>
  </section>;
}
