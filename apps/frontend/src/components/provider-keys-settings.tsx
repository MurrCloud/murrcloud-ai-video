"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { localTime } from "@/lib/workspace";
import type { KeyProvider, ProviderKeySettings, ProviderKeyStatus } from "@/lib/workspace";
import { Notice } from "./ui";
import { LoadingPanel, useActionLock } from "./workspace-common";

const labels: Record<KeyProvider, string> = { openai: "OpenAI", fal: "fal.ai" };
const path = "/settings/provider-keys";

function mutationError(error: unknown) {
  const status = error instanceof ApiError ? error.status : 0;
  if (status === 401) return "Your session has expired. Sign in again before changing provider keys.";
  if (status === 403) return "Administrator access and your correct current password are required.";
  if (status === 409) return "The saved keys changed. Review the refreshed status and enter your credentials again.";
  if (status === 422) return "Enter a valid API key and your current administrator password.";
  if (status === 429) return "Too many key changes. Wait a minute before trying again.";
  if (status === 503) return "Secure key storage is unavailable. Contact the Studio operator.";
  return "The key change could not be confirmed. Review the refreshed status before trying again.";
}

type KeyAction = (provider: ProviderKeyStatus, password: string, apiKey?: string) => Promise<void>;

function ProviderKeyForm({ provider, disabled, busy, reset, onAction }: { provider: ProviderKeyStatus; disabled: boolean; busy: boolean; reset: number; onAction: KeyAction }) {
  const [apiKey, setApiKey] = useState("");
  const [password, setPassword] = useState("");
  const label = labels[provider.provider];
  const validKey = /^[!-~]{16,512}$/.test(apiKey);
  const validPassword = password.length > 0 && password.length <= 256;
  useEffect(() => { setApiKey(""); setPassword(""); }, [reset]);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!disabled && validKey && validPassword) await onAction(provider, password, apiKey);
  }
  return <section aria-labelledby={`${provider.provider}-key-title`}>
    <h3 id={`${provider.provider}-key-title`}>{label}</h3>
    <dl className="settings-facts"><div><dt>Saved key</dt><dd>{provider.stored ? "Stored securely · awaiting approval" : "No saved key"}</dd></div>{provider.updated_at && <div><dt>Last changed</dt><dd>{localTime(provider.updated_at)}</dd></div>}</dl>
    <form aria-label={`${label} API key`} onSubmit={submit}>
      <fieldset disabled={disabled}>
        <label>{provider.stored ? `Replacement ${label} API key` : `${label} API key`}<input type="password" autoComplete="off" autoCapitalize="none" spellCheck={false} value={apiKey} onChange={event => setApiKey(event.target.value)} minLength={16} maxLength={512} placeholder="Paste a new API key"/><span className="field-help">Saved keys are never shown again. Leaving this blank preserves the saved key.</span></label>
        <label>Administrator password for {label}<input type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} maxLength={256}/></label>
        <div className="toolbar-actions"><button type="submit" className="button button-primary" disabled={!validKey || !validPassword}>{busy ? "Updating…" : provider.stored ? `Replace ${label} key` : `Save ${label} key`}</button>{provider.stored && <button type="button" className="button button-secondary" disabled={!validPassword || apiKey.length > 0} onClick={() => onAction(provider, password)}>Remove {label} key</button>}</div>
        {provider.stored && <p className="field-help">To remove this saved copy, leave the replacement field blank, enter your administrator password and choose Remove. This does not revoke the key at its provider.</p>}
      </fieldset>
    </form>
  </section>;
}

export function ProviderKeysSettings() {
  const [data, setData] = useState<ProviderKeySettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<KeyProvider | "refresh" | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [reset, setReset] = useState(0);
  const actionLock = useActionLock();
  useEffect(() => {
    let active = true;
    api<ProviderKeySettings>(path).then(value => { if (active) setData(value); }).catch(() => { if (active) setError("Provider key status could not be loaded. Refresh before making changes."); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);
  async function reload() {
    try { setData(await api<ProviderKeySettings>(path)); return true; }
    catch { setData(null); setError("Provider key status could not be refreshed. Refresh before making another change."); return false; }
  }
  async function refresh() {
    if (!actionLock.acquire()) return;
    setBusy("refresh"); setError(""); setMessage(""); setReset(value => value + 1);
    try { await reload(); } finally { actionLock.release(); setBusy(null); }
  }
  const act: KeyAction = async (provider, password, apiKey) => {
    if (!data?.storage_available || !actionLock.acquire()) return;
    setBusy(provider.provider); setError(""); setMessage(""); setReset(value => value + 1);
    try {
      const payload = { current_password: password, expected_revision: provider.revision, ...(apiKey === undefined ? {} : { api_key: apiKey }) };
      const result = await api<ProviderKeySettings>(`${path}/${provider.provider}`, { method: apiKey === undefined ? "DELETE" : "PUT", body: JSON.stringify(payload) });
      setData(result);
      setMessage(apiKey === undefined ? `${labels[provider.provider]} key removed. Paid generation settings are unchanged.` : `${labels[provider.provider]} key saved securely. No test request was sent and paid generation settings are unchanged.`);
    } catch (error) {
      setError(mutationError(error));
      await reload();
    } finally { setReset(value => value + 1); actionLock.release(); setBusy(null); }
  };
  return <section className="panel" aria-labelledby="provider-keys-heading">
    <h2 id="provider-keys-heading">Provider API keys</h2>
    <Notice>Saving a key does not enable paid generation or make a test request. Stored keys await operator approval.</Notice>
    <p className="field-help">Administrators can add, replace or remove encrypted server-side keys. Enter your current account password to confirm each change.</p>
    {error && <Notice kind="error">{error}</Notice>}{message && <Notice kind="success">{message}</Notice>}
    {loading ? <LoadingPanel>Loading provider key status…</LoadingPanel> : data && <>
      <dl className="settings-facts"><div><dt>Paid video generation</dt><dd>{data.paid_generation_enabled ? "Enabled by operator" : "Disabled"}</dd></div><div><dt>OpenAI director</dt><dd>{data.director_enabled ? "Enabled by operator" : "Disabled"}</dd></div></dl>
      {!data.storage_available && <Notice>Secure key storage is unavailable. Contact the Studio operator before adding keys.</Notice>}
      <div className="settings-grid">{data.providers.map(provider => <ProviderKeyForm key={provider.provider} provider={provider} disabled={!!busy || !data.storage_available} busy={busy === provider.provider} reset={reset} onAction={act}/>)}</div>
    </>}
    <button type="button" className="button button-secondary" disabled={loading || !!busy} onClick={refresh}>Refresh key status</button>
  </section>;
}
