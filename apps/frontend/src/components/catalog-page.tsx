"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api, errorMessage } from "@/lib/api";
import type { VideoModel } from "@/lib/types";
import { ModelSelector } from "./model-selector";
import { defaultModel, settingsForModel } from "@/lib/model-settings";
import type { ClipSettings } from "@/lib/types";
import { Icon, Notice } from "./ui";

export function CatalogPage() {
  const [models, setModels] = useState<VideoModel[]>([]);
  const [settings, setSettings] = useState<ClipSettings | null>(null);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  useEffect(() => { let active = true; api<VideoModel[]>("/models").then(value => { if (active) { setModels(value); const first = defaultModel(value); if (first) setSettings(settingsForModel(first)); } }).catch(error => { if (active) setError(errorMessage(error)); }).finally(() => { if (active) setLoaded(true); }); return () => { active = false; }; }, []);
  return <><div className="page-heading"><div><span className="eyebrow">THE RIGHT MODEL FOR THE MOMENT</span><h1>Explore your engines</h1><p>Compare verified capabilities before you start a production.</p></div><Link href="/projects/new" className="button button-primary"><Icon name="plus" size={17}/>Create a project</Link></div><Notice>Capability metadata comes from the verified catalog. Public prices are estimates, and do not confirm your account's final billing.</Notice>{error && <Notice kind="error">{error}</Notice>}{settings ? <section className="panel catalog-panel"><ModelSelector models={models} settings={settings} onChange={setSettings}/></section> : loaded ? <Notice>No enabled models are available. An administrator can seed or enable verified catalog entries.</Notice> : <div className="loading-panel" role="status"><span className="spinner"/>Loading models…</div>}</>;
}
