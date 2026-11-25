import type { Project, User } from "./types";

export type LibraryProject = Project & {
  archived: boolean; updated_at: string; scene_count: number;
  latest_output: { id: string; render_id: string; preview_url: string; download_url: string; is_current: boolean } | null;
};
export type PageResult<T> = { items: T[]; total: number; limit: number; offset: number };
export type StudioTemplate = { id: string; name: string; description: string; shared: boolean; revision: number; archived: boolean; owned: boolean; scene_count: number; target_duration_seconds: number | null; language: string | null; model_ids: string[]; plan_invalid?: boolean };
export type UsageTotals = { exposure_usd: string; remaining_approved_usd: string; remaining_budget_usd: string; total_actual_usd: string | null; known_cost_count: number; project_count: number; budget_usd: string; approved_cap_usd: string; committed_usd: string; confirmed_usd: string | null; unknown_cost_count: number; pending_cost_count: number; generation_jobs: number; director_jobs: number };
export type UsageGroup = { key: string; label: string; project_id?: string | null; scene_id?: string | null; reserved_usd: string; confirmed_usd: string | null; unknown_cost_count: number; pending_cost_count: number; job_count: number };
export type UsageReport = PageResult<UsageGroup> & { currency: "USD"; scope: "owned" | "all"; totals: UsageTotals };
export type DashboardData = { projects_total: number; in_production: number; completed: number; failed: number; recent_projects: LibraryProject[]; costs: { committed_usd: string; confirmed_usd: string | null; unknown_cost_count: number }; unresolved_jobs: number };
export type StudioSettings = { user: User; security: { session_hours: number; cookie_secure: boolean }; features: { paid_generation_enabled: boolean; director_enabled: boolean; narration_enabled: boolean; render_enabled: boolean; s3_enabled: boolean }; limits: Record<string, number>; providers: { fal_configured: boolean; openai_configured: boolean; tts_provider: string } };
export type KeyProvider = "openai" | "fal";
export type ProviderKeyStatus = { provider: KeyProvider; stored: boolean; revision: number; updated_at: string | null; runtime_configured: boolean; usage_enabled: boolean };
export type ProviderKeySettings = { storage_available: boolean; paid_generation_enabled: boolean; director_enabled: boolean; providers: ProviderKeyStatus[] };
export type OperationState = { status: "healthy" | "attention"; checked_at: string; admission: { allowed: boolean; reason: string }; storage: { used_bytes: number | null; max_bytes: number; min_free_bytes: number; free_bytes: number | null; available: boolean }; queues: { name: string; worker_count?: number; enabled?: boolean; queued: number; running: number; failed: number; oldest_pending_at: string | null }[]; alerts: { code: string; severity: string; count: number; message: string }[]; unresolved_paid_jobs: number; unknown_cost_count: number };
export type AuditItem = { id: string; event_type: string; project_id: string | null; entity_type: string | null; entity_id: string | null; created_at: string };

export function validBudget(value: string) {
  return /^\d{1,5}(?:\.\d{1,4})?$/.test(value.trim()) && Number(value) <= 10000;
}
export function pageLabel(offset: number, count: number, total: number) {
  return total === 0 ? "No results" : `${offset + 1}–${offset + count} of ${total}`;
}
export function bytes(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value) || value < 0) return "Unavailable";
  if (value < 1024) return `${Math.round(value)} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"]; let amount = value / 1024, index = 0;
  while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
  return `${amount.toFixed(1)} ${units[index]}`;
}
export function localTime(value: string | null | undefined) {
  if (!value || !Number.isFinite(Date.parse(value))) return "Unavailable";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}
export function workspaceIssue(value: string) {
  const messages: Record<string, string> = {
    revision_conflict: "The saved record changed. Reload it before applying another change.",
    project_changed: "The project changed. Reload it before applying another change.",
    project_metadata_conflict: "The project changed. Reload it before applying another change.",
    template_revision_conflict: "The template changed. Reload it before applying another change.",
    budget_below_existing_commitments: "The budget cannot be lower than the approved cap or spending exposure, including verified charge overruns.",
    shared_template_requires_admin_acknowledgement: "An administrator must review and acknowledge shared template content before saving.",
    unresolved_paid_jobs: "Resolve the project's active or uncertain generations before archiving.",
    render_already_active: "Wait for the current render before archiving this project.",
    unresolved_render_jobs: "Wait for unresolved rendering jobs before archiving this project.",
    project_archived: "Restore the project from the library before making changes.",
    reconcile_job_before_confirming_charge: "Reconcile or finish the provider job before confirming its final charge.",
    provider_request_mismatch: "The request ID differs from the one already recorded. Check the provider evidence.",
    provider_request_already_attached: "That provider request is already attached to another job. Review its identity.",
    provider_reconciliation_invalid: "The verified request ID could not be reconciled with this model and provider.",
    reconciliation_not_required: "This job no longer requires reconciliation. Reload the current record.",
    charge_already_confirmed: "This charge is already confirmed. Reload the current record.",
    idempotency_conflict: "This action key belongs to a different request. Reopen the form and review it again.",
  };
  return messages[value] ?? value.replaceAll("_", " ");
}

export type ReconciliationJob = { id: string; kind: "generation" | "director"; project_id: string; provider: string; model_id: string; status: string; updated_at: string; reserved_usd: string; provider_job_id: string | null };
export type UnconfirmedCost = { id: string; project_id: string; provider: string; model_id: string | null; generation_job_id: string | null; director_job_id: string | null; rendering_job_id: string | null; status: string; amount_usd: string | null; provider_charge_id: string | null; updated_at: string };
export function validVerifiedAmount(value: string) { return /^\d{1,7}(?:\.\d{1,6})?$/.test(value.trim()) && Number(value) <= 1000000; }
export function validProviderRequestId(value: string) { return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value.trim()); }
export function validEvidenceReference(value: string) { return value.trim().length >= 3 && value.length <= 240 && !/[\u0000-\u001f]/.test(value); }
