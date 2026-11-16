"""Session management for the Create Genie Agent.

Maintains conversation history and accumulated state per session.
Persists to the Workbench Delta state store when configured, falls back to in-memory.
"""

import asyncio
import json
import time
import logging
import secrets
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 3600  # 1 hour
MAX_SESSIONS = 50


@dataclass
class AgentSession:
    session_id: str
    history: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    space_config: dict | None = None
    space_id: str | None = None
    space_url: str | None = None
    selected_catalogs: list[str] = field(default_factory=list)
    selected_schemas: list[str] = field(default_factory=list)
    selected_tables: list[str] = field(default_factory=list)
    feasibility_confirmed: bool = False
    continuation_count: int = 0
    llm_model: str | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    def add_message(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})
        self.last_active = time.time()

    def add_tool_call(self, tool_call_id: str, name: str, arguments: str) -> None:
        self.history.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": tool_call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }],
        })
        self.last_active = time.time()

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        self.history.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        })
        self.last_active = time.time()

    def is_expired(self) -> bool:
        return (time.time() - self.last_active) > SESSION_TTL_SECONDS


# ---------------------------------------------------------------------------
# In-memory cache (always used as L1; the Delta state store is the durable L2)
# ---------------------------------------------------------------------------

_sessions: dict[str, AgentSession] = {}

_STORE = "agent_sessions"


async def _ensure_table() -> None:
    """Kept for lifecycle compatibility — the state store owns its table."""
    return None


def _to_record(session: AgentSession) -> dict:
    return {
        "session_id": session.session_id,
        "history": session.history,
        "space_config": session.space_config,
        "space_id": session.space_id,
        "space_url": session.space_url,
        "selected_catalogs": session.selected_catalogs,
        "selected_schemas": session.selected_schemas,
        "selected_tables": session.selected_tables,
        "feasibility_confirmed": session.feasibility_confirmed,
        "continuation_count": session.continuation_count,
        "llm_model": session.llm_model,
        "created_at": session.created_at,
        "last_active": session.last_active,
    }


def _from_record(rec: dict) -> AgentSession:
    return AgentSession(
        session_id=rec["session_id"],
        history=rec.get("history") or [],
        created_at=float(rec.get("created_at") or time.time()),
        last_active=float(rec.get("last_active") or time.time()),
        space_config=rec.get("space_config"),
        space_id=rec.get("space_id"),
        space_url=rec.get("space_url"),
        selected_catalogs=rec.get("selected_catalogs") or [],
        selected_schemas=rec.get("selected_schemas") or [],
        selected_tables=rec.get("selected_tables") or [],
        feasibility_confirmed=bool(rec.get("feasibility_confirmed")),
        continuation_count=int(rec.get("continuation_count") or 0),
        llm_model=rec.get("llm_model"),
    )


async def _persist(session: AgentSession) -> None:
    """Queue the session for a durable write (fire-and-forget safe)."""
    from backend.services import state_store

    try:
        await state_store.kv_put(_STORE, session.session_id, _to_record(session))
    except Exception as e:
        logger.warning(f"Failed to persist session {session.session_id}: {e}")


async def _load(session_id: str) -> AgentSession | None:
    """Try to load a session from the durable store."""
    from backend.services import state_store

    try:
        rec = await state_store.kv_get_durable(_STORE, session_id)
        if not rec:
            return None
        session = _from_record(rec)
        if session.is_expired():
            return None
        return session
    except Exception as e:
        logger.warning(f"Failed to load session {session_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# Public API (sync wrappers — the router can await the async variants)
# ---------------------------------------------------------------------------

def create_session() -> AgentSession:
    """Create a new agent session (in-memory). Call persist_session() after."""
    _evict_expired()
    session_id = secrets.token_hex(16)
    session = AgentSession(session_id=session_id)
    _sessions[session_id] = session
    logger.info(f"Created agent session {session_id}")
    return session


def get_session(session_id: str) -> AgentSession | None:
    """Get session from in-memory cache."""
    session = _sessions.get(session_id)
    if session is None:
        return None
    if session.is_expired():
        _sessions.pop(session_id, None)
        return None
    session.last_active = time.time()
    return session


async def get_session_async(session_id: str) -> AgentSession | None:
    """Get session: tries in-memory first, falls back to the durable store."""
    session = get_session(session_id)
    if session is not None:
        return session
    # L2: Delta state store
    session = await _load(session_id)
    if session is not None:
        session.last_active = time.time()
        _sessions[session_id] = session  # promote to L1
        logger.info(f"Restored session {session_id} from the state store")
    return session


async def persist_session(session: AgentSession) -> None:
    """Persist session to the durable store (non-blocking best-effort)."""
    await _persist(session)


def delete_session(session_id: str) -> bool:
    """Delete a session from in-memory cache. Returns True if it existed."""
    return _sessions.pop(session_id, None) is not None


async def delete_session_async(session_id: str) -> bool:
    """Delete from both the in-memory cache and the durable store."""
    from backend.services import state_store

    existed = _sessions.pop(session_id, None) is not None
    if not existed:
        existed = (await _load(session_id)) is not None
    try:
        await state_store.kv_delete(_STORE, session_id)
    except Exception as e:
        logger.warning(f"Failed to delete session {session_id}: {e}")
    return existed


def _evict_expired() -> None:
    """Remove expired sessions from in-memory cache and enforce max count."""
    expired = [sid for sid, s in _sessions.items() if s.is_expired()]
    for sid in expired:
        _sessions.pop(sid, None)

    if len(_sessions) >= MAX_SESSIONS:
        oldest = sorted(_sessions.items(), key=lambda x: x[1].last_active)
        for sid, _ in oldest[:len(_sessions) - MAX_SESSIONS + 1]:
            _sessions.pop(sid, None)
