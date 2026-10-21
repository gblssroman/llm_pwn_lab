import httpx
import pytest

from llm_convo.vm_master import VmMasterClient


@pytest.mark.asyncio
async def test_vm_master_client_uses_role_tokens_and_polls_jobs() -> None:
    requests: list[httpx.Request] = []
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        requests.append(request)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/attacks":
            assert request.headers["X-Lab-Token"] == "e" * 32
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "tool-misuse",
                        "title": "Tool misuse",
                        "category": "test",
                        "mode": "agent",
                    }
                ],
            )
        if request.url.path == "/v1/attacks/tool-misuse/jobs/sandbox":
            assert request.headers["X-Lab-Token"] == "s" * 32
            assert request.method == "POST"
            return httpx.Response(202, json={"id": "job-1"})
        if request.url.path == "/v1/jobs/job-1":
            assert request.headers["X-Lab-Token"] == "s" * 32
            polls += 1
            if polls == 1:
                return httpx.Response(
                    200,
                    json={
                        "status": "running",
                        "events": [{"seq": 1, "stream": "stdout", "text": "working"}],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "events": [],
                    "result": {
                        "exit_code": 0,
                        "stdout": "ok",
                        "stderr": "",
                        "timed_out": False,
                    },
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = VmMasterClient(
        base_url="http://vm-master:8000",
        evaluator_token="e" * 32,
        sandbox_token="s" * 32,
        job_poll_seconds=0.001,
        client=http,
    )
    logs: list[dict] = []

    assert await client.health() == {"status": "ok"}
    scenarios = await client.list_scenarios()
    terminal = await client.run_job(
        "tool-misuse",
        "sandbox",
        command="id",
        timeout_seconds=10,
        on_log=logs.append,
    )

    assert scenarios[0].id == "tool-misuse"
    assert terminal["stdout"] == "ok"
    assert logs == [{"seq": 1, "stream": "stdout", "text": "working"}]
    assert requests[-1].url.params["after"] == "1"
    await http.aclose()


@pytest.mark.asyncio
async def test_vm_master_client_waits_for_busy_resource() -> None:
    starts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal starts
        if request.url.path == "/v1/attacks/tool-misuse/jobs/down":
            starts += 1
            if starts == 1:
                return httpx.Response(409, json={"detail": "resource is busy"})
            return httpx.Response(202, json={"id": "job-down"})
        if request.url.path == "/v1/jobs/job-down":
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "events": [],
                    "result": {
                        "exit_code": 0,
                        "stdout": "down",
                        "stderr": "",
                        "timed_out": False,
                    },
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = VmMasterClient(
        base_url="http://vm-master:8000",
        evaluator_token="e" * 32,
        sandbox_token="s" * 32,
        job_poll_seconds=0.001,
        client=http,
    )

    result = await client.run_job(
        "tool-misuse",
        "down",
        job_timeout_seconds=1,
        wait_if_busy=True,
    )

    assert starts == 2
    assert result["stdout"] == "down"
    await http.aclose()
