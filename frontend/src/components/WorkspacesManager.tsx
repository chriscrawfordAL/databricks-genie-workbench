/**
 * Admin surface to register and manage remote workspace connections.
 *
 * Each connection targets a remote Databricks workspace via a service-principal
 * OAuth (M2M) credential. The client secret is sent once on create/rotate and
 * is never returned by the API. Adding/testing/removing here updates the
 * WorkspaceContext (which drives the header switcher).
 */

import { useState } from "react"
import {
  AlertTriangle, Building2, CheckCircle2, Loader2, Plus, RefreshCw, Trash2, XCircle,
} from "lucide-react"

import { createWorkspace, deleteWorkspace, testWorkspace } from "@/lib/api"
import { useWorkspace } from "@/hooks/WorkspaceContext"
import type { WorkspaceConnection, WorkspaceTestResult } from "@/types"

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

function AddWorkspaceForm({ onDone }: { onDone: () => void }) {
  const [name, setName] = useState("")
  const [host, setHost] = useState("")
  const [clientId, setClientId] = useState("")
  const [clientSecret, setClientSecret] = useState("")
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSaving(true)
    setError(null)
    try {
      await createWorkspace({ name, host, client_id: clientId, client_secret: clientSecret })
      onDone()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to add workspace")
    } finally {
      setSaving(false)
    }
  }

  const inputCls =
    "w-full px-3 py-2 rounded-lg bg-surface-secondary border border-default text-sm text-primary focus:outline-none focus:ring-1 focus:ring-accent"

  return (
    <form onSubmit={submit} className="bg-surface border border-default rounded-xl p-4 space-y-3">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label className="space-y-1">
          <span className="text-xs text-muted">Display name</span>
          <input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} placeholder="Prod (us-east-1)" required />
        </label>
        <label className="space-y-1">
          <span className="text-xs text-muted">Workspace host</span>
          <input className={inputCls} value={host} onChange={(e) => setHost(e.target.value)} placeholder="https://dbc-xxxx.cloud.databricks.com" required />
        </label>
        <label className="space-y-1">
          <span className="text-xs text-muted">Service principal client ID</span>
          <input className={inputCls} value={clientId} onChange={(e) => setClientId(e.target.value)} autoComplete="off" required />
        </label>
        <label className="space-y-1">
          <span className="text-xs text-muted">Client secret</span>
          <input className={inputCls} type="password" value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} autoComplete="new-password" required />
        </label>
      </div>
      {error && (
        <div className="flex items-start gap-2 text-sm text-red-400">
          <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
          <span>{error}</span>
        </div>
      )}
      <div className="flex items-center gap-2">
        <button
          type="submit"
          disabled={saving}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-accent text-white text-sm font-medium disabled:opacity-60"
        >
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
          {saving ? "Validating…" : "Add workspace"}
        </button>
        <button type="button" onClick={onDone} className="px-3 py-1.5 rounded-lg text-sm text-muted hover:text-secondary">
          Cancel
        </button>
      </div>
      <p className="text-xs text-muted">
        The credentials are validated against the remote workspace before saving. The secret is stored in a
        Databricks secret scope in this (home) workspace — never in the app database.
      </p>
    </form>
  )
}

function WorkspaceRow({ ws, onChanged }: { ws: WorkspaceConnection; onChanged: () => void }) {
  const [busy, setBusy] = useState<"test" | "delete" | null>(null)
  const [testResult, setTestResult] = useState<WorkspaceTestResult | null>(null)

  const runTest = async () => {
    setBusy("test")
    setTestResult(null)
    try {
      const r = await testWorkspace(ws.workspace_id)
      setTestResult(r)
      onChanged()
    } catch (e) {
      setTestResult({ ok: false, status: "error", detail: e instanceof Error ? e.message : "Test failed", system_tables: false })
    } finally {
      setBusy(null)
    }
  }

  const remove = async () => {
    if (!confirm(`Remove connection to "${ws.name}"? Its stored secret will be deleted.`)) return
    setBusy("delete")
    try {
      await deleteWorkspace(ws.workspace_id)
      onChanged()
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="bg-surface border border-default rounded-xl p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-3 min-w-0">
          <Building2 className="w-5 h-5 text-muted mt-0.5 flex-shrink-0" />
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="font-medium text-primary truncate">{ws.name}</span>
              <StatusPill status={ws.status} />
            </div>
            <div className="text-xs text-muted truncate">{ws.host}</div>
            <div className="text-xs text-muted truncate">client_id: {ws.client_id}</div>
            {ws.status_detail && <div className="text-xs text-muted mt-1">{ws.status_detail}</div>}
          </div>
        </div>
        <div className="flex items-center gap-1 flex-shrink-0">
          <button
            onClick={runTest}
            disabled={busy !== null}
            className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-sm text-secondary hover:bg-surface-secondary disabled:opacity-60"
            title="Test connection"
          >
            {busy === "test" ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            Test
          </button>
          <button
            onClick={remove}
            disabled={busy !== null}
            className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-sm text-red-400 hover:bg-red-500/10 disabled:opacity-60"
            title="Remove connection"
          >
            {busy === "delete" ? <Loader2 className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
          </button>
        </div>
      </div>
      {testResult && (
        <div className={`mt-3 text-sm flex items-start gap-2 ${testResult.ok ? "text-emerald-500" : "text-red-400"}`}>
          {testResult.ok ? <CheckCircle2 className="w-4 h-4 mt-0.5" /> : <XCircle className="w-4 h-4 mt-0.5" />}
          <span>{testResult.detail}</span>
        </div>
      )}
    </div>
  )
}

export function WorkspacesManager() {
  const { workspaces, loading, refreshWorkspaces } = useWorkspace()
  const [adding, setAdding] = useState(false)

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-display font-semibold text-primary">Remote workspaces</h3>
          <p className="text-sm text-muted">
            Deploy, score, and monitor Genie Agents in other Databricks workspaces. Auto-Optimize runs only in the
            current (home) workspace.
          </p>
        </div>
        {!adding && (
          <button
            onClick={() => setAdding(true)}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-accent text-white text-sm font-medium"
          >
            <Plus className="w-4 h-4" />
            Add workspace
          </button>
        )}
      </div>

      {adding && (
        <AddWorkspaceForm
          onDone={() => {
            setAdding(false)
            void refreshWorkspaces()
          }}
        />
      )}

      {loading ? (
        <div className="bg-surface border border-default rounded-xl p-8 flex items-center justify-center text-muted">
          <Loader2 className="w-5 h-5 animate-spin" />
        </div>
      ) : workspaces.length === 0 && !adding ? (
        <div className="bg-surface border border-default rounded-xl p-8 text-center text-muted text-sm">
          No remote workspaces registered. Add one to deploy, score, and monitor Genie Agents across workspaces.
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
