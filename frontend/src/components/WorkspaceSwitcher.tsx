/**
 * Header dropdown to pick the target workspace: the home (current) workspace
 * or any registered remote connection. Selection flows through
 * WorkspaceContext, which updates the api-layer target header.
 */

import { useEffect, useRef, useState } from "react"
import { Building2, Check, ChevronDown, Home } from "lucide-react"

import { useWorkspace } from "@/hooks/WorkspaceContext"

function statusDotClass(status: string): string {
  if (status === "active") return "bg-emerald-500"
  if (status === "active_no_system_tables") return "bg-amber-500"
  if (status === "error") return "bg-red-500"
  return "bg-gray-400"
}

export function WorkspaceSwitcher() {
  const { workspaces, selectedWorkspaceId, selectedWorkspace, selectWorkspace } = useWorkspace()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener("mousedown", onClick)
    return () => document.removeEventListener("mousedown", onClick)
  }, [open])

  // No remote workspaces registered → nothing to switch to; hide entirely.
  if (workspaces.length === 0) return null

  const label = selectedWorkspace ? selectedWorkspace.name : "Current workspace"

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium text-secondary hover:bg-surface-secondary transition-colors max-w-[220px]"
        title="Target workspace"
      >
        {selectedWorkspace ? (
          <Building2 className="w-4 h-4 flex-shrink-0" />
        ) : (
          <Home className="w-4 h-4 flex-shrink-0" />
        )}
        <span className="truncate">{label}</span>
        <ChevronDown className="w-3.5 h-3.5 flex-shrink-0 opacity-60" />
      </button>

      {open && (
        <div className="absolute right-0 mt-1 w-64 rounded-lg bg-surface shadow-lg ring-1 ring-black/5 dark:ring-white/10 z-50 py-1">
          <button
            onClick={() => {
              selectWorkspace(null)
              setOpen(false)
            }}
            className="w-full flex items-center gap-2 px-3 py-2 text-sm text-secondary hover:bg-surface-secondary text-left"
          >
            <Home className="w-4 h-4 flex-shrink-0" />
            <span className="flex-1 truncate">Current workspace</span>
            {selectedWorkspaceId === null && <Check className="w-4 h-4 text-accent" />}
          </button>

          <div className="my-1 border-t border-border" />

          {workspaces.map((ws) => (
            <button
              key={ws.workspace_id}
              onClick={() => {
                selectWorkspace(ws.workspace_id)
                setOpen(false)
              }}
              className="w-full flex items-center gap-2 px-3 py-2 text-sm text-secondary hover:bg-surface-secondary text-left"
            >
              <span
                className={`w-2 h-2 rounded-full flex-shrink-0 ${statusDotClass(ws.status)}`}
                title={ws.status}
              />
              <span className="flex-1 min-w-0">
                <span className="block truncate">{ws.name}</span>
                <span className="block truncate text-xs text-muted">{ws.host}</span>
              </span>
              {selectedWorkspaceId === ws.workspace_id && (
                <Check className="w-4 h-4 text-accent flex-shrink-0" />
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
