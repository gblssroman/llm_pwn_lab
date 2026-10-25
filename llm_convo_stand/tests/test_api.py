import httpx
import pytest
from fastapi.testclient import TestClient

from llm_convo.api import _public_config, create_app
from llm_convo.config import Settings
from llm_convo.schemas import VmPoolItem, VmScenario, VmScenarioSummary


def test_frontend_and_empty_history_are_served(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'api.sqlite3'}"
    app = create_app(
        Settings(
            database_url=database_url,
            langfuse_public_key=None,
            langfuse_secret_key=None,
            worker_poll_seconds=60,
        )
    )

    with TestClient(app) as client:
        frontend = client.get("/")
        stylesheet = client.get("/assets/app.css")
        javascript = client.get("/assets/app.js")
        markdown = client.get("/assets/markdown.js")
        health = client.get("/health/ready")
        history = client.get("/v1/conversations")

    assert frontend.status_code == 200
    assert "Agent PWN Lab" in frontend.text
    assert "Agent Relay" not in frontend.text
    assert 'id="agent-relay-tab"' in frontend.text
    assert 'id="vm-manager-tab"' in frontend.text
    assert 'id="vm-manager-view"' in frontend.text
    assert "Turn on all" in frontend.text
    assert "Shutdown all" in frontend.text
    assert 'name="agent_a_base_url"' in frontend.text
    assert 'name="agent_b_base_url"' in frontend.text
    assert 'name="agent_a_api_key" type="password"' in frontend.text
    assert 'name="agent_b_api_key" type="password"' in frontend.text
    assert 'name="agent_a_model"' in frontend.text
    assert 'name="agent_b_model"' in frontend.text
    assert 'name="agent_a_system_prompt"' in frontend.text
    assert 'name="agent_b_system_prompt"' in frontend.text
    assert "Additional instructions" not in frontend.text
    assert 'name="agent_a_top_p" type="number" min="0.01" max="1" step="0.01"' in frontend.text
    assert 'name="agent_b_top_p" type="number" min="0.01" max="1" step="0.01"' in frontend.text
    assert frontend.text.count("Generation and context settings") == 2
    assert frontend.headers["cache-control"] == "no-cache"
    assert stylesheet.status_code == 200
    assert ".message-stream" in stylesheet.text
    assert "overflow-y: auto" in stylesheet.text
    assert "@keyframes" not in stylesheet.text
    assert "animation:" not in stylesheet.text
    assert javascript.status_code == 200
    assert "function updateLiveMessage" in javascript.text
    assert "function conversationTimeline" in javascript.text
    assert '"agent_react_step"' in javascript.text
    assert '"tool_started"' in javascript.text
    assert "function populateScenarioPrompts" in javascript.text
    assert 'system_prompt: values[`${prefix}_system_prompt`].trim()' in javascript.text
    assert 'api_key: values[`${prefix}_api_key`] || null' in javascript.text
    assert "additional_instructions" not in javascript.text
    assert "followLiveOutput" in javascript.text
    assert "event.deltaY < 0" in javascript.text
    assert markdown.status_code == 200
    assert "function renderMarkdown" in markdown.text
    assert "escapeHtml" in markdown.text
    assert "function renderTable" in markdown.text
    assert ".markdown-table-wrap" in stylesheet.text
    assert "/assets/markdown.js?v=vm-benchmark-1" in frontend.text
    assert '<option value="benchmark">VM benchmark scenario</option>' in frontend.text
    assert 'name="max_tool_calls_per_b_turn"' in frontend.text
    assert "Finish &amp; evaluate" in frontend.text
    assert "Agent A system prompt" in frontend.text
    assert "Agent B system prompt" in frontend.text
    assert 'id="prompt-dialog"' in frontend.text
    assert 'id="prompt-dialog-content"' in frontend.text
    assert "function openPromptDialog" in javascript.text
    assert "function fmtDateTime" in javascript.text
    assert "const scenarioFor = (item)" in javascript.text
    assert "fmtDateTime(item.created_at)" in javascript.text
    assert "height: min(60vh, 560px)" in stylesheet.text
    assert 'name="task_prompt"' not in frontend.text
    assert "system-dot" not in frontend.text
    assert "Backend status" in frontend.text
    assert "Made by" in frontend.text
    assert "https://github.com/gblssroman" in frontend.text
    assert "https://github.com/redrumerr" in frontend.text
    assert 'id="database-status"' in frontend.text
    assert 'id="langfuse-status"' in frontend.text
    assert "database-label" not in frontend.text
    assert "langfuse-label" not in frontend.text
    assert "function selectWorkspace" in javascript.text
    assert "function renderVmPool" in javascript.text
    assert 'api("/v1/vm-pool")' in javascript.text
    assert health.json() == {"status": "ok", "database": "ok", "langfuse": "disabled"}
    assert history.status_code == 200
    assert history.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


def test_public_conversation_config_redacts_direct_api_keys() -> None:
    stored = {
        "agent_a": {"model": "a", "api_key": "secret-a", "api_key_env": None},
        "agent_b": {"model": "b", "api_key": "secret-b", "api_key_env": "B_KEY"},
        "summarizer": {"model": "s", "api_key": "secret-s"},
        "benchmark_scenario_id": "docker-socket",
    }

    public = _public_config(stored)

    assert all("api_key" not in public[name] for name in ("agent_a", "agent_b", "summarizer"))
    assert public["agent_b"]["api_key_env"] == "B_KEY"
    assert stored["agent_a"]["api_key"] == "secret-a"


def test_conversation_api_never_returns_direct_api_keys(tmp_path) -> None:
    app = create_app(
        Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'secrets.sqlite3'}",
            langfuse_public_key=None,
            langfuse_secret_key=None,
            worker_poll_seconds=60,
        )
    )
    agent = {
        "system_prompt": "Keep this credential private.",
        "model": "remote-model",
        "base_url": "https://llm.example/v1",
        "api_key": "never-return-this",
        "api_key_env": None,
    }

    with TestClient(app) as client:
        app.state.runner.enqueue = lambda _conversation_id: None
        created = client.post(
            "/v1/conversations",
            headers={"Idempotency-Key": "secret-redaction"},
            json={"agent_a": agent, "agent_b": agent},
        )
        viewed = client.get(f"/v1/conversations/{created.json()['conversation_id']}")

    assert created.status_code == 202
    assert viewed.status_code == 200
    assert "never-return-this" not in viewed.text
    assert "api_key" not in viewed.json()["config"]["agent_a"]
    assert "api_key" not in viewed.json()["config"]["agent_b"]


def test_benchmark_catalog_filters_unsupported_memory_poisoning(tmp_path) -> None:
    class FakeVmMaster:
        configured = True

        async def health(self):
            return {"status": "ok"}

        async def list_scenarios(self):
            return [
                VmScenarioSummary(
                    id="tool-misuse", title="Tool misuse", category="test", mode="agent"
                ),
                VmScenarioSummary(
                    id="memory-poisoning",
                    title="Memory poisoning",
                    category="test",
                    mode="agent",
                ),
            ]

        async def get_scenario(self, scenario_id):
            return VmScenario(
                id=scenario_id,
                title="Tool misuse",
                category="test",
                mode="agent",
                prompt_a="Scenario A instructions.",
                prompt_b="Scenario B instructions.",
            )

    app = create_app(
        Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'catalog.sqlite3'}",
            langfuse_public_key=None,
            langfuse_secret_key=None,
            worker_poll_seconds=60,
        )
    )

    with TestClient(app) as client:
        app.state.vm_master = FakeVmMaster()
        health = client.get("/v1/vm-master/health")
        catalog = client.get("/v1/benchmark-scenarios")
        unsupported = client.get("/v1/benchmark-scenarios/memory-poisoning")

    assert health.json() == {"configured": True, "status": "ok", "error": None}
    assert [item["id"] for item in catalog.json()] == ["tool-misuse"]
    assert unsupported.status_code == 422


@pytest.mark.asyncio
async def test_vm_pool_is_proxied_through_agent_relay(tmp_path) -> None:
    calls: list[tuple[str, str | None, str | None]] = []

    def item(*, desired: str = "cold", state: str = "cold") -> VmPoolItem:
        return VmPoolItem(
            scenario_id="docker-socket",
            title="Docker socket",
            mode="escape",
            desired=desired,
            state=state,
            updated_at="2026-10-24T12:00:00Z",
            memory_mb=4096,
        )

    class FakeVmMaster:
        configured = True

        async def list_vm_pool(self):
            calls.append(("list", None, None))
            return [item()]

        async def change_vm_pool(self, action):
            calls.append(("all", None, action))
            desired = "warm" if action == "warm-all" else "cold"
            state = "queued" if desired == "warm" else "resetting"
            return [item(desired=desired, state=state)]

        async def change_vm_pool_item(self, scenario_id, action):
            calls.append(("item", scenario_id, action))
            desired = "warm" if action == "warm" else "cold"
            state = "queued" if desired == "warm" else "resetting"
            return item(desired=desired, state=state)

    app = create_app(
        Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'vm-pool.sqlite3'}",
            langfuse_public_key=None,
            langfuse_secret_key=None,
            worker_poll_seconds=0.01,
        )
    )

    app.state.vm_master = FakeVmMaster()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/v1/vm-pool")
        warmed = await client.post("/v1/vm-pool/docker-socket/warm")
        stopped = await client.post("/v1/vm-pool/docker-socket/shutdown")
        all_warmed = await client.post("/v1/vm-pool/warm-all")
        all_stopped = await client.post("/v1/vm-pool/shutdown-all")

    assert listed.status_code == 200
    assert listed.json()[0]["state"] == "cold"
    assert warmed.status_code == 202
    assert warmed.json()["desired"] == "warm"
    assert stopped.status_code == 202
    assert stopped.json()["desired"] == "cold"
    assert all_warmed.status_code == 202
    assert all_stopped.status_code == 202
    assert calls == [
        ("list", None, None),
        ("item", "docker-socket", "warm"),
        ("item", "docker-socket", "shutdown"),
        ("all", None, "warm-all"),
        ("all", None, "shutdown-all"),
    ]
