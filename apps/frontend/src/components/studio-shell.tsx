"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useContext, useEffect, useState } from "react";
import { api, ApiError, errorMessage } from "@/lib/api";
import type { User } from "@/lib/types";
import { Brand, Icon, Notice } from "./ui";

const UserContext = createContext<User | null>(null);
export const useUser = () => useContext(UserContext);

export function StudioShell({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const pathname = usePathname();
  const router = useRouter();
  useEffect(() => {
    let active = true;
    api<User>("/auth/me").then(value => { if (active) setUser(value); }).catch(error => {
      if (!active) return;
      if (error instanceof ApiError && error.status === 401) router.replace("/login");
      else setError(errorMessage(error));
    });
    return () => { active = false; };
  }, [router]);

  async function signOut() {
    setBusy(true);
    try { await api<void>("/auth/logout", { method: "POST" }); router.replace("/login"); }
    catch (error) { setError(errorMessage(error)); setBusy(false); }
  }

  return <UserContext.Provider value={user}><div className="studio-layout">
    <aside className="sidebar">
      <Link href="/" className="brand-link" aria-label="MurrCloud Video Studio home"><Brand/></Link>
      <div className="workspace-label">PRODUCTION WORKSPACE</div>
      <nav aria-label="Main navigation">
        <Link href="/" className={`nav-link ${pathname === "/" ? "selected" : ""}`}><Icon name="grid"/>Dashboard</Link>
        <Link href="/projects/new/storyboard" className={`nav-link ${pathname.endsWith("/storyboard") ? "selected" : ""}`}><Icon name="spark"/>Plan a video</Link>
        <Link href="/projects/new" className={`nav-link ${pathname === "/projects/new" ? "selected" : ""}`}><Icon name="plus"/>Generate a clip</Link>
        <Link href="/library" className={`nav-link ${pathname === "/library" ? "selected" : ""}`}><Icon name="film"/>Project library</Link>
        <Link href="/templates" className={`nav-link ${pathname === "/templates" ? "selected" : ""}`}><Icon name="models"/>Templates</Link>
        <Link href="/usage" className={`nav-link ${pathname === "/usage" ? "selected" : ""}`}><Icon name="grid"/>Usage and costs</Link>
        <Link href="/settings" className={`nav-link ${pathname === "/settings" ? "selected" : ""}`}><Icon name="lock"/>Settings</Link>
        {user?.role === "admin" && <Link href="/operations" className={`nav-link ${pathname === "/operations" ? "selected" : ""}`}><Icon name="alert"/>Operations</Link>}
        <Link href="/models" className={`nav-link ${pathname === "/models" ? "selected" : ""}`}><Icon name="models"/>Model catalog</Link>
      </nav>
      <div className="sidebar-bottom"><span className="safe-dot"/><div><strong>Internal studio</strong><p>Cloud generation · local control</p></div></div>
    </aside>
    <div className="workspace">
      <header className="topbar"><span className="topbar-label">MURRCLOUD / VIDEO STUDIO</span><div className="account"><span className="internal-badge">INTERNAL</span>{user && <><span className="avatar">{user.email[0].toUpperCase()}</span><span className="account-email">{user.email}</span><button onClick={signOut} disabled={busy} className="icon-button" aria-label="Sign out"><Icon name="logout" size={18}/></button></>}</div></header>
      <main className="main-content">{error && <Notice kind="error">{error} <button className="text-button" onClick={() => window.location.reload()}>Retry</button></Notice>}{user ? children : !error ? <div className="loading-panel" role="status"><span className="spinner"/>Connecting to your workspace…</div> : null}</main>
      <footer className="footer"><span>MurrCloud AI Video Studio</span><span>Model selection. Clear costs. Creative control.</span></footer>
    </div>
  </div></UserContext.Provider>;
}
