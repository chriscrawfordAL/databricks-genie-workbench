/**
 * DataTransferTab — cross-workspace data-transfer observability.
 *
 * Shows how much data the app has pulled IN from each remote workspace so an
 * admin can estimate egress cost. The backend measures app-observed response
 * bytes at the outbound call sites (Genie config fetch, SQL results, system-
 * table monitoring reads) attributed to the target workspace — a proxy for the
 * cloud provider's egress bill, not the billed figure.
 *
 * Egress rates vary by cloud and by customer discount, so the user enters their
 * own $/GB rate (persisted locally) and the tab computes an estimated cost.
 */
import { useEffect, useMemo, useState } from "react"
import { RefreshCw, Info } from "lucide-react"

import { Card } from "@/components/ui/card"
import { Stat } from "@/watch/components/Stat"
import { LineChart } from "@/watch/components/LineChart"
import { getTransferSummary } from "@/lib/api"
import type { TransferSummary } from "@/types"

// Decimal units so the displayed GB equals the GB used in the cost math
// (cloud egress is billed per 10^9-byte GB).
const GB = 1_000_000_000
const RATE_STORAGE_KEY = "genie-workbench-egress-rate"

function formatBytes(n: number): string {
  if (!n) return "0 B"
  const units = ["B", "KB", "MB", "GB", "TB"]
  let i = 0
  let v = n
  while (v >= 1000 && i < units.length - 1) {
    v /= 1000
    i++
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : v >= 10 ? 1 : 2)} ${units[i]}`
}

function toGB(bytes: number): number {
  return bytes / GB
}

function formatUsd(v: number): string {
  return v.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: v < 1 ? 4 : 2 })
}

function readStoredRate(): string {
  try {
    return localStorage.getItem(RATE_STORAGE_KEY) || ""
  } catch {
    return ""
  }
}

export function DataTransferTab() {
  const [days, setDays] = useState<number>(30)
  const [summary, setSummary] = useState<TransferSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [rateInput, setRateInput] = useState<string>(readStoredRate)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      setLoading(true)
      setError(null)
      try {
        const s = await getTransferSummary(days)
        if (!cancelled) setSummary(s)
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load transfer data")
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => { cancelled = true }
  }, [days])

  useEffect(() => {
    try {
      if (rateInput.trim()) localStorage.setItem(RATE_STORAGE_KEY, rateInput.trim())
      else localStorage.removeItem(RATE_STORAGE_KEY)
    } catch { /* ignore storage errors */ }
  }, [rateInput])

  const rate = useMemo(() => {
    const n = parseFloat(rateInput)
    return Number.isFinite(n) && n >= 0 ? n : null
  }, [rateInput])

  const totalBytes = summary?.total_bytes ?? 0
  const estCost = rate !== null ? toGB(totalBytes) * rate : null

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Data Transfer</h1>
          <p className="text-sm text-muted">
            Data pulled in from remote workspaces — an egress-cost estimate for cross-workspace use.
          </p>
        </div>
        <div className="flex items-center gap-3">
          {loading && (
            <span className="flex items-center gap-1 text-xs text-muted">
              <RefreshCw size={12} className="animate-spin" /> Updating…
            </span>
          )}
          <select
            value={days}
            onChange={e => setDays(Number(e.target.value))}
            className="rounded border border-default bg-elevated px-2 py-1 text-sm"
          >
            <option value={7}>last 7 days</option>
            <option value={30}>last 30 days</option>
            <option value={90}>last 90 days</option>
          </select>
        </div>
      </div>

      <div className="flex items-start gap-2 rounded-lg border border-default bg-surface p-3 text-xs text-muted">
        <Info size={14} className="mt-0.5 shrink-0" />
        <p>
          App-observed bytes received from remote workspaces (Genie Agent config, SQL results, and
          monitoring reads), a proxy for cloud egress — not the provider's billed figure. Traffic
          within the home workspace isn't counted, since egress applies only across regions/clouds.
          Enter your egress rate below to estimate cost; rates vary by cloud provider and discount.
        </p>
      </div>

      {error && (
        <Card className="p-4 text-sm text-red-500">Couldn't load transfer data: {error}</Card>
      )}

      <div className={`grid grid-cols-2 gap-3 transition-opacity sm:grid-cols-4 ${loading ? "opacity-50" : ""}`}>
        <Stat label="Total pulled in" value={summary ? formatBytes(totalBytes) : "—"} />
        <Stat label="≈ GB" value={summary ? toGB(totalBytes).toLocaleString(undefined, { maximumFractionDigits: 3 }) : "—"} />
        <Stat label="Remote requests" value={summary ? summary.total_requests.toLocaleString() : "—"} />
        <Stat
          label="Est. egress cost"
          value={estCost !== null ? formatUsd(estCost) : <span className="text-base text-muted">enter rate →</span>}
        />
      </div>

      <Card className="flex flex-wrap items-center gap-3 p-4">
        <label className="text-sm font-medium">Egress rate</label>
        <div className="flex items-center gap-1">
          <span className="text-sm text-muted">$</span>
          <input
            type="number"
            min={0}
            step="0.001"
            inputMode="decimal"
            placeholder="0.09"
            value={rateInput}
            onChange={e => setRateInput(e.target.value)}
            className="w-28 rounded border border-default bg-elevated px-2 py-1 text-sm tabular-nums"
          />
          <span className="text-sm text-muted">per GB</span>
        </div>
        <span className="text-xs text-muted">
          GB = 10⁹ bytes. Your cost basis differs by cloud (e.g. AWS/Azure/GCP cross-region) and contract.
        </span>
      </Card>

      <Card className={`p-4 transition-opacity ${loading ? "opacity-50" : ""}`}>
        <h2 className="mb-3 text-sm font-medium uppercase text-muted">
          Daily data pulled in — last {days} days
        </h2>
        {summary && summary.daily.length
          ? <LineChart data={summary.daily.map(d => ({ x: d.day, y: toGB(d.bytes) }))} formatY={v => `${v.toFixed(2)} GB`} />
          : <p className="text-sm text-muted">{loading ? "Loading…" : "No cross-workspace transfer recorded yet."}</p>}
      </Card>

      <Card className="overflow-hidden p-0">
        <h2 className="border-b border-default px-4 py-3 text-sm font-medium uppercase text-muted">
          By workspace
        </h2>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="border-b border-default bg-elevated text-left text-xs uppercase text-muted">
              <tr>
                <th className="px-4 py-2">Workspace</th>
                <th className="px-4 py-2 text-right">Total</th>
                <th className="px-4 py-2 text-right">Genie</th>
                <th className="px-4 py-2 text-right">SQL</th>
                <th className="px-4 py-2 text-right">Monitoring</th>
                <th className="px-4 py-2 text-right">Requests</th>
                <th className="px-4 py-2 text-right">Est. cost</th>
              </tr>
            </thead>
            <tbody>
              {summary?.workspaces.map(w => {
                const wsCost = rate !== null ? toGB(w.total_bytes) * rate : null
                return (
                  <tr key={w.workspace_id} className="border-t border-default/50">
                    <td className="px-4 py-2">
                      <div className="font-medium">{w.name || "(unregistered)"}</div>
                      <div className="font-mono text-xs text-muted">{w.host || w.workspace_id}</div>
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums">{formatBytes(w.total_bytes)}</td>
                    <td className="px-4 py-2 text-right tabular-nums text-muted">{formatBytes(w.by_category.genie)}</td>
                    <td className="px-4 py-2 text-right tabular-nums text-muted">{formatBytes(w.by_category.sql)}</td>
                    <td className="px-4 py-2 text-right tabular-nums text-muted">{formatBytes(w.by_category.system_tables)}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{w.requests.toLocaleString()}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{wsCost !== null ? formatUsd(wsCost) : "—"}</td>
                  </tr>
                )
              })}
              {summary && !summary.workspaces.length && (
                <tr><td colSpan={7} className="p-6 text-center text-muted">No cross-workspace transfer recorded yet. Deploy, score, or monitor an agent in a remote workspace to populate this.</td></tr>
              )}
              {!summary && (
                <tr><td colSpan={7} className="p-6 text-center text-muted">Loading…</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  )
}
