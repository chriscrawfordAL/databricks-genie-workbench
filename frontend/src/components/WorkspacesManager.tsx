/**
 * Admin surface to register and manage other workspaces in the same account.
 *
 * Each connection reaches a remote Databricks workspace as a service principal
 * (OAuth M2M), either the app's own service principal (added to the remote
 * workspace by an admin) or a dedicated service principal whose client secret
 * is sent once and stored in a secret scope in this (home) workspace. Secrets
 * are never returned by the API. Changes here refresh the WorkspaceContext,
 * which drives the header workspace switcher.
 */

import { useEffect, useState } from "react"
import {
  AlertTriangle, Building2, CheckCircle2, Copy, KeyRound, Loader2, Pencil, Plus, RefreshCw, Trash2, XCircle,
} from "lucide-react"

import {
  createWorkspace, deleteWorkspace, getWorkspacesInfo, testWorkspace, updateWorkspace,
} from "@/lib/api"
import { useWorkspace } from "@/hooks/WorkspaceContext"
import type {
  WorkspaceAuthMode, WorkspaceConnection, WorkspacesInfo, WorkspaceTestResult,
} from "@/types"

const inputCls =
  "w-full px-3 py-2 rounded-lg bg-surface-secondary border border-default text-sm text-primary focus:outline-none focus:ring-1 focus:ring-accent"

function StatusPill({ status }: { status: string }) {
  const map: Record<string, { label: string; cls: string; icon: React.ReactNode }> = {
    active: { label: "Active", cls: "bg-emerald-500/10 text-emerald-500", icon: <CheckCircle2 className="w-3.5 h-3.5" /> },
    active_no_system_tables: { label: "No system tables", cls: "bg-amber-500/10 text-amber-500", icon: <AlertTriangle className="w-3.5 h-3.5" /> },
    error: { label: "Error", cls: "bg-red-500/10 text-red-500", icon: <XCircle className="w-3.5 h-3.5" /> },
  }
  const s = map[status] || { label: status || "Unknown", cls: "bg-gray-500/10 text-gray-400", icon: null }
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${s.cls}`}>
      {s.icon}
      {s.label}
    </span>
  )
}

function CopyValue({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      type="button"
      onClick={() => {
        void navigator.clipboard?.writeText(value).then(() => {
          setCopied(true)
          setTimeout(() => setCopied(false), 1500)
        })
      }}
      className="inline-flex items-center gap-1 font-mono text-xs px-1.5 py-0.5 rounded bg-surface-secondary border border-default text-secondary hover:text-primary"
      title="Copy"
    >
      {value}
      {copied ? <CheckCircle2 className="w-3 h-3 text-emerald-500" /> : <Copy className="w-3 h-3" />}
    </button>
  )
}

function ErrorLine({ message }: { message: string }) {
  return (
    <div className="flex items-start gap-2 text-sm text-red-400">
      <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
      <span>{message}</span>
    </div>
  )
}

function AddWorkspaceForm({ info, onDone }: { info: WorkspacesInfo | null; onDone: (saved: boolean) => void }) {
  const [authMode, setAuthMode] = useState<WorkspaceAuthMode>(info?.app_sp_client_id ? "app_sp" : "custom_sp")
  const [name, setName] = useState("")
  const [host, setHost] = useState("")
  const [clientId, setClientId] = useState("")
  const [clientSecret, setClientSecret] = useState("")
  const [warehouseId, setWarehouseId] = useState("")
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSaving(true)
    setError(null)
    try {
      await createWorkspace({
        name,
        host,
        auth_mode: authMode,
        ...(authMode === "custom_sp" ? { client_id: clientId, client_secret: clientSecret } : {}),
        ...(warehouseId.trim() ? { warehouse_id: warehouseId.trim() } : {}),
      })
      onDone(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to add workspace")
    } finally {
      setSaving(false)
    }
  }

  return (
    <form onSubmit={submit} className="bg-surface border border-default rounded-xl p-4 space-y-4">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label className="space-y-1">
          <span className="text-xs text-muted">Display name</span>
          <input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} placeholder="Analytics (us-west4)" required />
        </label>
        <label className="space-y-1">
          <span className="text-xs text-muted">Workspace URL</span>
          <input className={inputCls} value={host} onChange={(e) => setHost(e.target.value)} placeholder="https://1234567890.1.gcp.databricks.com" required />
        </label>
      </div>

      <fieldset className="space-y-2">
        <legend className="text-xs text-muted mb-1">Connect as</legend>
        <label className={`flex items-start gap-2 text-sm ${info?.app_sp_client_id ? "" : "opacity-50"}`}>
          <input
            type="radio"
            name="auth_mode"
            className="mt-1"
            checked={authMode === "app_sp"}
            disabled={!info?.app_sp_client_id}
            onChange={() => setAuthMode("app_sp")}
          />
          <span>
            <span className="text-primary font-medium">This app's service principal</span>
            <span className="block text-xs text-muted">
              No secret to manage. A workspace admin first adds the service principal
              {info?.app_sp_client_id ? <> <CopyValue value={info.app_sp_client_id} /> </> : " "}
              to the other workspace (Settings → Identity and access → Service principals → Add existing).
            </span>
          </span>
        </label>
        <label className="flex items-start gap-2 text-sm">
          <input type="radio" name="auth_mode" className="mt-1" checked={authMode === "custom_sp"} onChange={() => setAuthMode("custom_sp")} />
          <span>
            <span className="text-primary font-medium">A dedicated service principal</span>
            <span className="block text-xs text-muted">
              Use a service principal with an OAuth secret that already has access in the other workspace.
            </span>
          </span>
        </label>
      </fieldset>

      {authMode === "custom_sp" && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <label className="space-y-1">
            <span className="text-xs text-muted">Service principal client ID</span>
            <input className={inputCls} value={clientId} onChange={(e) => setClientId(e.target.value)} autoComplete="off" required />
          </label>
          <label className="space-y-1">
            <span className="text-xs text-muted">OAuth client secret</span>
            <input className={inputCls} type="password" value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} autoComplete="new-password" required />
          </label>
        </div>
      )}

      <label className="space-y-1 block">
        <span className="text-xs text-muted">SQL warehouse ID in that workspace (optional)</span>
        <input className={inputCls} value={warehouseId} onChange={(e) => setWarehouseId(e.target.value)} placeholder="Auto-detect a Pro or Serverless warehouse" autoComplete="off" />
      </label>

      {error && <ErrorLine message={error} />}
      <div className="flex items-center gap-2">
        <button
          type="submit"
          disabled={saving}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-accent text-white text-sm font-medium disabled:opacity-60"
        >
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
          {saving ? "Connecting…" : "Add workspace"}
        </button>
        <button type="button" onClick={() => onDone(false)} className="px-3 py-1.5 rounded-lg text-sm text-muted hover:text-secondary">
          Cancel
        </button>
      </div>
      <p className="text-xs text-muted">
        The app signs in to the other workspace before saving anything.
        {authMode === "custom_sp" && <> The secret is kept in the <span className="font-mono">{info?.secret_scope ?? "genie-workbench-workspaces"}</span> secret scope in this workspace, never in the app's tables.</>}
      </p>
    </form>
  )
}

function EditWorkspaceForm({ ws, onDone }: { ws: WorkspaceConnection; onDone: (saved: boolean) => void }) {
  const [name, setName] = useState(ws.name)
  const [warehouseId, setWarehouseId] = useState(ws.warehouse_id ?? "")
  const [clientSecret, setClientSecret] = useState("")
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSaving(true)
    setError(null)
    try {
      await updateWorkspace(ws.workspace_id, {
        ...(name !== ws.name ? { name } : {}),
        ...(warehouseId !== (ws.warehouse_id ?? "") ? { warehouse_id: warehouseId.trim() } : {}),
        ...(clientSecret ? { client_secret: clientSecret } : {}),
      })
      onDone(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update workspace")
    } finally {
      setSaving(false)
    }
  }

  return (
    <form onSubmit={submit} className="mt-3 border-t border-default pt-3 space-y-3">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label className="space-y-1">
          <span className="text-xs text-muted">Display name</span>
          <input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} required />
        </label>
        <label className="space-y-1">
          <span className="text-xs text-muted">SQL warehouse ID (blank = auto-detect)</span>
          <input className={inputCls} value={warehouseId} onChange={(e) => setWarehouseId(e.target.value)} autoComplete="off" />
        </label>
        {ws.auth_mode === "custom_sp" && (
          <label className="space-y-1 sm:col-span-2">
            <span className="text-xs text-muted">New client secret (leave blank to keep the current one)</span>
            <input className={inputCls} type="password" value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} autoComplete="new-password" />
          </label>
        )}
      </div>
      {error && <ErrorLine message={error} />}
      <div className="flex items-center gap-2">
        <button type="submit" disabled={saving} className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-accent text-white text-sm font-medium disabled:opacity-60">
          {saving && <Loader2 className="w-4 h-4 animate-spin" />}
          Save
        </button>
        <button type="button" onClick={() => onDone(false)} className="px-3 py-1.5 rounded-lg text-sm text-muted hover:text-secondary">
          Cancel
        </button>
      </div>
    </form>
  )
}

function WorkspaceRow({ ws, onChanged }: { ws: WorkspaceConnection; onChanged: () => void }) {
  const [busy, setBusy] = useState<"test" | "delete" | null>(null)
  const [editing, setEditing] = useState(false)
  const [testResult, setTestResult] = useState<WorkspaceTestResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  const runTest = async () => {
    setBusy("test")
    setTestResult(null)
    try {
      setTestResult(await testWorkspace(ws.workspace_id))
      onChanged()
    } catch (e) {
      setTestResult({ ok: false, status: "error", detail: e instanceof Error ? e.message : "Test failed", system_tables: false, warehouses: 0 })
    } finally {
      setBusy(null)
    }
  }

  const remove = async () => {
    const what = ws.auth_mode === "custom_sp" ? " Its stored secret will be deleted." : ""
    if (!confirm(`Remove the connection to "${ws.name}"?${what}`)) return
    setBusy("delete")
    setError(null)
    try {
      await deleteWorkspace(ws.workspace_id)
      onChanged()
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to remove the connection")
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="bg-surface border border-default rounded-xl p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-3 min-w-0">
          <Building2 className="w-5 h-5 text-muted mt-0.5 flex-shrink-0" />
          <div className="min-w-0 space-y-0.5">
            <div className="flex items-center gap-2">
              <span className="font-medium text-primary truncate">{ws.name}</span>
              <StatusPill status={ws.status} />
            </div>
            <div className="text-xs text-muted truncate">{ws.host}</div>
            <div className="text-xs text-muted truncate flex items-center gap-1">
              <KeyRound className="w-3 h-3" />
              {ws.auth_mode === "app_sp" ? "This app's service principal" : `Service principal ${ws.client_id ?? ""}`}
              {ws.remote_workspace_id && <> · workspace {ws.remote_workspace_id}</>}
              {ws.warehouse_id && <> · warehouse {ws.warehouse_id}</>}
            </div>
            {ws.status_detail && <div className="text-xs text-muted">{ws.status_detail}</div>}
          </div>
        </div>
        <div className="flex items-center gap-1 flex-shrink-0">
          <button onClick={runTest} disabled={busy !== null} className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-sm text-secondary hover:bg-surface-secondary disabled:opacity-60" title="Test connection">
            {busy === "test" ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            Test
          </button>
          <button onClick={() => setEditing((v) => !v)} disabled={busy !== null} className="inline-flex items-center px-2.5 py-1.5 rounded-lg text-sm text-secondary hover:bg-surface-secondary disabled:opacity-60" title="Edit connection">
            <Pencil className="w-4 h-4" />
          </button>
          <button onClick={remove} disabled={busy !== null} className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-sm text-red-400 hover:bg-red-500/10 disabled:opacity-60" title="Remove connection">
            {busy === "delete" ? <Loader2 className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
          </button>
        </div>
      </div>
      {editing && (
        <EditWorkspaceForm
          ws={ws}
          onDone={(saved) => {
            setEditing(false)
            if (saved) onChanged()
          }}
        />
      )}
      {error && <div className="mt-3"><ErrorLine message={error} /></div>}
      {testResult && (
        <div className={`mt-3 text-sm flex items-start gap-2 ${testResult.ok ? "text-emerald-500" : "text-red-400"}`}>
          {testResult.ok ? <CheckCircle2 className="w-4 h-4 mt-0.5" /> : <XCircle className="w-4 h-4 mt-0.5" />}
          <span>
            {testResult.detail}
            {testResult.ok && testResult.user && <span className="text-muted"> (signed in as {testResult.user})</span>}
          </span>
        </div>
      )}
    </div>
  )
}

export function WorkspacesManager() {
  const { workspaces, loading, refreshWorkspaces } = useWorkspace()
  const [adding, setAdding] = useState(false)
  const [info, setInfo] = useState<WorkspacesInfo | null>(null)

  useEffect(() => {
    let cancelled = false
    getWorkspacesInfo()
      .then((i) => { if (!cancelled) setInfo(i) })
      .catch(() => { /* non-admins never reach this tab */ })
    return () => { cancelled = true }
  }, [])

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="text-lg font-display font-semibold text-primary">Other workspaces</h3>
          <p className="text-sm text-muted max-w-3xl">
            List, scan, create, monitor and optimize Genie Agents in other workspaces of this Databricks account.
            Pick a workspace from the switcher in the header to work there. The app connects as a service principal,
            so that principal needs access to the agents (Can Manage to optimize), their tables, a SQL warehouse,
            and the <span className="font-mono">system</span> schema for monitoring.
          </p>
        </div>
        {!adding && (
          <button onClick={() => setAdding(true)} className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-accent text-white text-sm font-medium flex-shrink-0">
            <Plus className="w-4 h-4" />
            Add workspace
          </button>
        )}
      </div>

      {adding && (
        <AddWorkspaceForm
          info={info}
          onDone={(saved) => {
            setAdding(false)
            if (saved) void refreshWorkspaces()
          }}
        />
      )}

      {loading ? (
        <div className="bg-surface border border-default rounded-xl p-8 flex items-center justify-center text-muted">
          <Loader2 className="w-5 h-5 animate-spin" />
        </div>
      ) : workspaces.length === 0 && !adding ? (
        <div className="bg-surface border border-default rounded-xl p-8 text-center text-muted text-sm">
          No other workspaces registered yet.
        </div>
      ) : (
        <div className="space-y-3">
          {workspaces.map((ws) => (
            <WorkspaceRow key={ws.workspace_id} ws={ws} onChanged={() => void refreshWorkspaces()} />
          ))}
        </div>
      )}
    </div>
  )
}
