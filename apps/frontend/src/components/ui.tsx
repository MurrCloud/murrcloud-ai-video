import type { ReactNode } from "react";

export function Icon({ name, size = 20, ...props }: { name: string; size?: number } & React.SVGProps<SVGSVGElement>) {
  const paths: Record<string, ReactNode> = {
    grid: <><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></>,
    plus: <><path d="M12 5v14M5 12h14"/></>,
    film: <><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 8h4M3 12h4M3 16h4M17 8h4M17 12h4M17 16h4"/></>,
    arrow: <><path d="M5 12h14M14 7l5 5-5 5"/></>,
    check: <path d="m5 12 4 4L19 6"/>,
    clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
    spark: <><path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3ZM20 2v4M18 4h4"/></>,
    models: <><path d="m12 3 9 5-9 5-9-5 9-5ZM3 12l9 5 9-5M3 16l9 5 9-5"/></>,
    lock: <><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3"/></>,
    download: <><path d="M12 3v12m-4-4 4 4 4-4M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/></>,
    refresh: <><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 7a7 7 0 0 1 11-2l3 7M4 12l3 7a7 7 0 0 0 11-2"/></>,
    logout: <><path d="M9 4H4v16h5M10 12h11m-4-4 4 4-4 4"/></>,
    alert: <><path d="m12 3 10 18H2L12 3Z"/><path d="M12 9v5M12 17v.1"/></>,
    play: <path d="m9 5 11 7-11 7V5Z"/>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>{paths[name] ?? paths.film}</svg>;
}

export function Brand() {
  return <span className="brand"><span className="brand-mark"><Icon name="play" size={19}/></span><span>MurrCloud<span className="brand-caption">AI VIDEO STUDIO</span></span></span>;
}

export function Notice({ children, kind = "info" }: { children: ReactNode; kind?: "info" | "error" | "success" }) {
  return <div className={`notice notice-${kind}`} role={kind === "error" ? "alert" : "status"}><Icon name={kind === "error" ? "alert" : kind === "success" ? "check" : "lock"} size={18}/><div>{children}</div></div>;
}

export function StatusPill({ status }: { status: string }) {
  const label = status.replaceAll("_", " ");
  const tone = ["completed", "succeeded", "clips_ready"].includes(status) ? "success" : ["failed", "reconciliation_required"].includes(status) ? "danger" : ["draft", "cancelled", "abandoned"].includes(status) ? "neutral" : "active";
  return <span className={`status status-${tone}`}><span/>{label}</span>;
}

export function EmptyState({ title, children, action }: { title: string; children: ReactNode; action?: ReactNode }) {
  return <div className="empty-state"><span className="empty-icon"><Icon name="film" size={30}/></span><h2>{title}</h2><p>{children}</p>{action}</div>;
}
