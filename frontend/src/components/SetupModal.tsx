/**
 * SetupModal — first-run storage setup.
 *
 * Genie Workbench keeps its own state in a Unity Catalog Delta table. On first
 * launch no location is chosen yet: a workspace admin picks the catalog and
 * schema here. Browsing, creating the schema, and granting the app's service
 * principal access run with the admin's own permissions; the service principal
 * owns the data from then on. Non-admins see a wait message and can dismiss it.
 */
import { useEffect, useState } from "react"
import { Database, Loader2, ShieldAlert, X } from "lucide-react"
import { configureStorage, listSetupCatalogs, listSetupSchemas } from "@/lib/api"
import type { StorageStatus } from "@/types"

interface Props {
  status: StorageStatus
  onComplete: () => void
  onDismiss: () => void
}

export function SetupModal({ status, onComplete, onDismiss }: Props) {
  const [catalogs, setCatalogs] = useState<string[] | null>(null)
  const [catalog, setCatalog] = useState<string>("")
  const [schemas, setSchemas] = useState<string[] | null>(null)
  const [schemaMode, setSchemaMode] = useState<"existing" | "new">("existing")
  const [existingSchema, setExistingSchema] = useState<string>("")
  const [newSchema, setNewSchema] = useState<string>(status.suggested_schema || "genie_workbench_state")
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Load catalogs once (admins only).
  useEffect(() => {
    if (!status.is_admin) return
    let cancelled = false
    listSetupCatalogs()
      .then((rows) => {
        if (cancelled) return
        const names = rows.map((r) => r.name)
        setCatalogs(names)
        if (status.suggested_catalog && names.includes(status.suggested_catalog)) {
          setCatalog(status.suggested_catalog)
        }
      })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load catalogs") })
    return () => { cancelled = true }
  }, [status.is_admin, status.suggested_catalog])

  // Load schemas whenever the catalog changes.
  useEffect(() => {
    if (!catalog) return
    let cancelled = false
    const load = async () => {
      setSchemas(null)
      setExistingSchema("")
      try {
        const rows = await listSetupSchemas(catalog)
        if (cancelled) return
        const names = rows.map((r) => r.name)
        setSchemas(names)
        // Default to creating the suggested schema unless it already exists.
        const suggested = status.suggested_schema || "genie_workbench_state"
        if (names.includes(suggested)) {
          setSchemaMode("existing")
          setExistingSchema(suggested)
        } else {
          setSchemaMode("new")
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load schemas")
      }
    }
    void load()
    return () => { cancelled = true }
  }, [catalog, status.suggested_schema])

  const schema = schemaMode === "new" ? newSchema.trim() : existingSchema
  const canSubmit = !submitting && !!catalog && !!schema

  async function handleSubmit() {
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      await configureStorage({ catalog, schema, create_schema: schemaMode === "new" })
      onComplete()
    } catch (e) {
      setError(e instanceof Error ? e.message : "Setup failed")
      setSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/50 p-4 backdrop-blur-sm">
      <div className="w-full max-w-lg rounded-2xl border border-default bg-surface shadow-xl">
        <div className="flex items-center gap-3 border-b border-default px-6 py-4">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-accent/10 text-accent">
            <Database className="h-5 w-5" />
          </div>
          <div className="flex-1">
            <h2 className="text-lg font-semibold text-primary">Set up storage</h2>
            <p className="text-xs text-muted">One-time setup for where Genie Workbench keeps its data.</p>
          </div>
          <button
            onClick={onDismiss}
            className="rounded-lg p-1.5 text-muted hover:bg-elevated hover:text-primary"
            title="Not now — the app works, but history won't be kept across restarts"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {!status.is_admin ? (
          <div className="px-6 py-8 text-center">
            <ShieldAlert className="mx-auto mb-3 h-8 w-8 text-amber-500" />
            <p className="text-sm text-primary">A workspace admin needs to finish setup.</p>
            <p className="mt-1 text-xs text-muted">
              Until then the app works, but scan history, stars and settings are not kept across restarts.
              Ask a workspace admin to open this app and choose where its data is stored.
            </p>
          </div>
        ) : (
          <div className="space-y-5 px-6 py-5">
            <p className="text-sm text-muted">
              Choose a Unity Catalog schema for the app's Delta table. Setup uses your permissions to
              create the schema (if needed) and to grant the app's service principal access; the app
              manages the data from then on.
            </p>

            {status.last_error && (
              <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
                Storage at {status.catalog}.{status.schema} is not reachable: {status.last_error}
              </div>
            )}

            {/* Catalog */}
            <label className="block">
              <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-muted">Catalog</span>
              <select
                value={catalog}
                onChange={(e) => setCatalog(e.target.value)}
                className="w-full rounded-lg border border-default bg-elevated px-3 py-2 text-sm"
                disabled={catalogs === null}
              >
                <option value="">{catalogs === null ? "Loading catalogs…" : "Select a catalog…"}</option>
                {catalogs?.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </label>

            {/* Schema */}
            {catalog && (
              <div className="space-y-2">
                <span className="block text-xs font-medium uppercase tracking-wide text-muted">Schema</span>
                <label className="flex items-center gap-2 text-sm">
                  <input type="radio" checked={schemaMode === "existing"} onChange={() => setSchemaMode("existing")} />
                  Use an existing schema
                </label>
                {schemaMode === "existing" && (
                  <select
                    value={existingSchema}
                    onChange={(e) => setExistingSchema(e.target.value)}
                    className="w-full rounded-lg border border-default bg-elevated px-3 py-2 text-sm"
                    disabled={schemas === null}
                  >
                    <option value="">{schemas === null ? "Loading schemas…" : "Select a schema…"}</option>
                    {schemas?.map((s) => <option key={s} value={s}>{s}</option>)}
                  </select>
                )}
                <label className="flex items-center gap-2 text-sm">
                  <input type="radio" checked={schemaMode === "new"} onChange={() => setSchemaMode("new")} />
                  Create a new schema
                </label>
                {schemaMode === "new" && (
                  <input
                    type="text"
                    value={newSchema}
                    onChange={(e) => setNewSchema(e.target.value)}
                    placeholder="genie_workbench_state"
                    className="w-full rounded-lg border border-default bg-elevated px-3 py-2 font-mono text-sm"
                  />
                )}
              </div>
            )}

            {catalog && schema && (
              <p className="text-xs text-muted">
                Data will be stored in <span className="font-mono text-secondary">{catalog}.{schema}.workbench_state</span>.
              </p>
            )}

            {error && (
              <div className="rounded-lg border border-red-500/30 bg-red-500/5 px-3 py-2 text-xs text-red-500">{error}</div>
            )}

            <button
              onClick={handleSubmit}
              disabled={!canSubmit}
              className="flex w-full items-center justify-center gap-2 rounded-lg bg-accent px-4 py-2.5 text-sm font-medium text-white transition-opacity disabled:opacity-50"
            >
              {submitting ? <><Loader2 className="h-4 w-4 animate-spin" /> Setting up…</> : "Complete setup"}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
