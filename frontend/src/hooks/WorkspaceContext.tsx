/* eslint-disable react-refresh/only-export-components */
/**
 * Cross-workspace targeting context.
 *
 * Holds the list of registered remote workspace connections and the currently
 * selected target. `null` means the home (current) workspace. The selection is
 * persisted to localStorage and mirrored into the api-layer singleton
 * (`setActiveWorkspaceId`) so the non-React fetch helpers attach the
 * `X-Target-Workspace` header automatically.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react"

import { listWorkspaces, setActiveWorkspaceId } from "@/lib/api"
import type { WorkspaceConnection } from "@/types"

const STORAGE_KEY = "genie-workbench-target-workspace"

interface WorkspaceContextValue {
  /** Registered remote connections (excludes the implicit home workspace). */
  workspaces: WorkspaceConnection[]
  /** Selected remote workspace id, or null for the home workspace. */
  selectedWorkspaceId: string | null
  /** The selected connection object, or null when home is selected. */
  selectedWorkspace: WorkspaceConnection | null
  /** True when a remote workspace is selected. */
  isRemote: boolean
  loading: boolean
  selectWorkspace: (workspaceId: string | null) => void
  refreshWorkspaces: () => Promise<void>
}

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null)

function readStoredSelection(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY) || null
  } catch {
    return null
  }
}

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [workspaces, setWorkspaces] = useState<WorkspaceConnection[]>([])
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<string | null>(
    readStoredSelection,
  )
  const [loading, setLoading] = useState(true)

  // Keep the api-layer singleton in sync on every selection change.
  useEffect(() => {
    setActiveWorkspaceId(selectedWorkspaceId)
  }, [selectedWorkspaceId])

  const refreshWorkspaces = useCallback(async () => {
    setLoading(true)
    try {
      const list = await listWorkspaces()
      setWorkspaces(list)
      // If the stored selection no longer exists, fall back to home.
      setSelectedWorkspaceId((prev) =>
        prev && !list.some((w) => w.workspace_id === prev) ? null : prev,
      )
    } catch {
      setWorkspaces([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refreshWorkspaces()
  }, [refreshWorkspaces])

  const selectWorkspace = useCallback((workspaceId: string | null) => {
    setSelectedWorkspaceId(workspaceId)
    setActiveWorkspaceId(workspaceId)
    try {
      if (workspaceId) localStorage.setItem(STORAGE_KEY, workspaceId)
      else localStorage.removeItem(STORAGE_KEY)
    } catch {
      /* ignore storage errors */
    }
    // Switching the target workspace is a full context change — every view
    // (Agents, Admin, Watch tabs) fetches its own data and won't otherwise
    // re-query under the new target. Reload so all data reflects the selection.
    // The selection is already persisted to localStorage and re-applied on boot.
    try {
      window.location.reload()
    } catch {
      /* non-browser env */
    }
  }, [])

  const value = useMemo<WorkspaceContextValue>(() => {
    const selectedWorkspace =
      workspaces.find((w) => w.workspace_id === selectedWorkspaceId) || null
    return {
      workspaces,
      selectedWorkspaceId,
      selectedWorkspace,
      isRemote: selectedWorkspaceId !== null && selectedWorkspace !== null,
      loading,
      selectWorkspace,
      refreshWorkspaces,
    }
  }, [workspaces, selectedWorkspaceId, loading, selectWorkspace, refreshWorkspaces])

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>
}

export function useWorkspace(): WorkspaceContextValue {
  const ctx = useContext(WorkspaceContext)
  if (!ctx) {
    throw new Error("useWorkspace must be used within a WorkspaceProvider")
  }
  return ctx
}
