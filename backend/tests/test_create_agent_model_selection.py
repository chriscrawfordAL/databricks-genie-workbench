import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routers import create
from backend.services import create_agent_session
from backend.services.create_agent_session import AgentSession


def test_agent_chat_request_model_is_saved_on_session(monkeypatch):
    captured: dict[str, object] = {}

    class FakeAgent:
        async def chat(self, session, user_message, selections=None, vc_capture=None):
            captured["session"] = session
            captured["model"] = session.llm_model
            captured["message"] = user_message
            yield {"event": "done", "data": {"needs_continuation": False}}

    monkeypatch.setattr("backend.services.create_agent.get_create_agent", lambda: FakeAgent())
    monkeypatch.setattr("backend.services.model_catalog.validate_chat_model", lambda model, client=None: model)
    monkeypatch.setattr("backend.services.auth.get_workspace_client", lambda: object())
    # Model validation runs against the home workspace (serving lives there).
    monkeypatch.setattr(
        "backend.services.auth.get_home_workspace_client", lambda: object(), raising=False,
    )
    monkeypatch.setattr(create, "persist_session", lambda session: None, raising=False)

    app = FastAPI()
    app.include_router(create.router)

    with TestClient(app) as client:
        resp = client.post(
            "/api/create/agent/chat",
            json={"message": "build a space", "model": "custom-chat"},
        )

    assert resp.status_code == 200, resp.text
    assert "event: done" in resp.text
    assert captured["model"] == "custom-chat"
    assert isinstance(captured["session"], AgentSession)


def test_agent_chat_rejects_a_malformed_space_id():
    app = FastAPI()
    app.include_router(create.router)

    with TestClient(app) as client:
        resp = client.post(
            "/api/create/agent/chat",
            json={"message": "hi", "space_id": "../x"},
        )

    assert resp.status_code == 422


def test_agent_chat_continuation_is_not_gated(monkeypatch):
    asked: list[tuple[str, object]] = []

    async def record_ask(space_id, level):
        asked.append((space_id, level))

    class FakeAgent:
        async def chat(self, session, user_message, selections=None, vc_capture=None):
            yield {"event": "done", "data": {"needs_continuation": False}}

    session = AgentSession(session_id="cont-1", space_id="space-seeded")

    async def load_session(session_id):
        assert session_id == "cont-1"
        return session

    monkeypatch.setattr(create, "require_space_access", record_ask)
    monkeypatch.setattr(
        "backend.services.create_agent.get_create_agent", lambda: FakeAgent()
    )
    monkeypatch.setattr(
        "backend.services.create_agent_session.get_session_async", load_session
    )
    monkeypatch.setattr(
        "backend.services.model_catalog.validate_chat_model",
        lambda model, client=None: model,
    )
    monkeypatch.setattr("backend.services.auth.get_workspace_client", lambda: object())
    monkeypatch.setattr(create, "persist_session", lambda session: None, raising=False)

    app = FastAPI()
    app.include_router(create.router)

    with TestClient(app) as client:
        resp = client.post(
            "/api/create/agent/chat",
            json={
                "session_id": "cont-1",
                "message": "",
                "space_id": "space-seeded",
            },
        )

    assert resp.status_code == 200, resp.text
    assert "event: done" in resp.text
    assert asked == []


def test_agent_session_persistence_round_trips_llm_model(monkeypatch):
    """A session (incl. its llm_model) survives a flush to Delta and a cold read."""
    from backend.services import delta_store, state_store

    table: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(state_store, "_lazy_pending", {})
    monkeypatch.setattr(state_store, "_dirty", {})
    monkeypatch.setattr(state_store, "_ready", True)
    monkeypatch.setattr(delta_store, "available", lambda: True)
    monkeypatch.setattr(
        delta_store, "upsert",
        lambda store, items: table.update({(store, k): v for k, v in items}),
    )
    monkeypatch.setattr(delta_store, "delete", lambda store, keys: None)
    monkeypatch.setattr(delta_store, "get_value", lambda store, key: table.get((store, key)))

    async def run():
        await create_agent_session._ensure_table()
        session = AgentSession(session_id="s1", llm_model="persisted-chat")
        await create_agent_session._persist(session)
        await state_store.flush()
        assert state_store._lazy_pending.get("agent_sessions") == {}
        return await create_agent_session._load("s1")

    loaded = asyncio.run(run())

    stored = json.loads(table[("agent_sessions", "s1")])
    assert stored["llm_model"] == "persisted-chat"
    assert loaded is not None
    assert loaded.llm_model == "persisted-chat"


def test_selectable_models_env_restricts_picker_and_keeps_default(monkeypatch):
    import pytest
    from backend.services import model_catalog

    monkeypatch.setenv("LLM_MODEL", "databricks-gpt-oss-120b")
    monkeypatch.setenv("GENIE_SELECTABLE_MODELS", "databricks-meta-llama-3-3-70b-instruct")
    names = [m.name for m in model_catalog.list_chat_models()]
    assert names == ["databricks-gpt-oss-120b", "databricks-meta-llama-3-3-70b-instruct"]
    assert model_catalog.validate_chat_model("databricks-gpt-oss-120b") == "databricks-gpt-oss-120b"
    with pytest.raises(model_catalog.ModelValidationError):
        model_catalog.validate_chat_model("databricks-claude-opus-5-5")
    monkeypatch.setenv("GENIE_SELECTABLE_MODELS", "__GENIE_SELECTABLE_MODELS__")
    assert "databricks-claude-opus-5-5" in [m.name for m in model_catalog.list_chat_models()]
