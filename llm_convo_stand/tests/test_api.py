from fastapi.testclient import TestClient

from llm_convo.api import create_app
from llm_convo.config import Settings
from llm_convo.schemas import VmScenario, VmScenarioSummary


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
    assert "Agent Relay" in frontend.text
    assert 'name="agent_a_base_url"' in frontend.text
    assert 'name="agent_b_base_url"' in frontend.text
    assert 'name="agent_a_model"' in frontend.text
    assert 'name="agent_b_model"' in frontend.text
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
    assert 'name="task_prompt"' not in frontend.text
    assert "system-dot" not in frontend.text
    assert "Backend status" in frontend.text
    assert 'id="database-status"' in frontend.text
    assert 'id="langfuse-status"' in frontend.text
    assert "database-label" not in frontend.text
    assert "langfuse-label" not in frontend.text
    assert health.json() == {"status": "ok", "database": "ok", "langfuse": "disabled"}
    assert history.status_code == 200
    assert history.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


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
