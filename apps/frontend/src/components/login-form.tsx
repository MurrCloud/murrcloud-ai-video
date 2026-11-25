"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api, errorMessage } from "@/lib/api";
import type { User } from "@/lib/types";
import { Brand, Icon, Notice } from "./ui";

export function LoginForm() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try { await api<User>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }); setPassword(""); router.replace("/"); }
    catch (error) { setError(errorMessage(error)); setPassword(""); setBusy(false); }
  }
  return <div className="login-layout"><div className="login-story"><Brand/><div className="login-story-copy"><span className="eyebrow">FROM WORDS TO MOTION</span><h1>Your vision.<br/>A new frame.</h1><p>A dedicated space to turn ideas into video, choose the right model, and keep every generation under control.</p><div className="story-feature"><Icon name="models"/>Selectable cloud models</div><div className="story-feature"><Icon name="film"/>Scene-by-scene production</div><div className="story-feature"><Icon name="lock"/>Approval before spending</div></div><span className="login-copyright">MURRCLOUD · AI VIDEO STUDIO</span></div><main className="login-main"><form onSubmit={submit} className="login-card"><span className="eyebrow">YOUR PRODUCTION WORKSPACE</span><h2>Welcome to the studio</h2><p className="muted">Sign in with your internal MurrCloud Video Studio account.</p>{error && <Notice kind="error">{error}</Notice>}<label>Email address<input type="email" value={email} onChange={e => setEmail(e.target.value)} required maxLength={320} autoComplete="username" placeholder="you@company.com"/></label><label>Password<input type="password" value={password} onChange={e => setPassword(e.target.value)} required maxLength={256} autoComplete="current-password"/></label><button className="button button-primary full-width" disabled={busy}>{busy ? "Signing in…" : <>Enter the studio<Icon name="arrow" size={18}/></>}</button><p className="small muted login-help"><Icon name="lock" size={14}/>Accounts are provisioned by your studio administrator.</p></form></main></div>;
}
