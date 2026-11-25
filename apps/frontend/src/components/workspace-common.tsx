"use client";
import { useEffect, useRef } from "react";
import { pageLabel } from "@/lib/workspace";

export function useMutationKey() {
  const key = useRef<{ fingerprint: string; value: string } | null>(null);
  return {
    get(payload: unknown) { const fingerprint = JSON.stringify(payload); if (key.current?.fingerprint !== fingerprint) key.current = { fingerprint, value: crypto.randomUUID() }; return key.current.value; },
    clear() { key.current = null; },
  };
}
export function useActionLock() {
  const pending = useRef(false);
  return { acquire() { if (pending.current) return false; pending.current = true; return true; }, release() { pending.current = false; } };
}
export function useActionPanelFocus(value: unknown) {
  const panel = useRef<HTMLElement>(null);
  useEffect(() => { if (value) panel.current?.querySelector<HTMLElement>("input, textarea, select")?.focus(); }, [value]);
  return panel;
}
export function Pagination({ offset, count, total, limit, busy, onPage }: { offset: number; count: number; total: number; limit: number; busy: boolean; onPage: (offset: number) => void }) {
  return <nav className="list-pagination" aria-label="Results pages"><span>{pageLabel(offset, count, total)}</span><div><button className="button button-secondary button-small" disabled={busy || offset === 0} onClick={() => onPage(Math.max(0, offset - limit))}>Previous</button><button className="button button-secondary button-small" disabled={busy || offset + count >= total} onClick={() => onPage(offset + limit)}>Next</button></div></nav>;
}
export function LoadingPanel({ children = "Loading your workspace…" }: { children?: React.ReactNode }) { return <div className="loading-panel" role="status"><span className="spinner"/>{children}</div>; }
