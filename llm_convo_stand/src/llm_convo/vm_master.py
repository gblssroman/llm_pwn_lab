import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import httpx

from llm_convo.schemas import VmScenario, VmScenarioSummary

Role = Literal["evaluator", "sandbox"]
JobAction = Literal["up", "down", "verify", "sandbox", "status"]
LogCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class VmMasterError(RuntimeError):
    pass


class VmMasterNotConfiguredError(VmMasterError):
    pass


class VmMasterBusyError(VmMasterError):
    pass


class VmMasterJobError(VmMasterError):
    def __init__(self, message: str, job: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.job = job or {}


class VmMasterClient:
    """Typed, server-side client for the unchanged vm_master_stand HTTP API."""

    def __init__(
        self,
        *,
        base_url: str | None,
        evaluator_token: str | None,
        sandbox_token: str | None,
        request_timeout_seconds: float = 30.0,
        job_poll_seconds: float = 1.0,
        job_timeout_seconds: float = 1_200.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") if base_url else None
        self._evaluator_token = evaluator_token
        self._sandbox_token = sandbox_token
        self._request_timeout = request_timeout_seconds
        self._job_poll_seconds = job_poll_seconds
        self._job_timeout_seconds = job_timeout_seconds
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    @property
    def configured(self) -> bool:
        return bool(
            self.base_url
            and self._evaluator_token
            and self._sandbox_token
            and self._evaluator_token != self._sandbox_token
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _require_configured(self) -> None:
        if not self.configured:
            raise VmMasterNotConfiguredError(
                "vm_master is not configured; set its base URL and both role tokens"
            )

    def _headers(self, role: Role) -> dict[str, str]:
        self._require_configured()
        token = self._sandbox_token if role == "sandbox" else self._evaluator_token
        return {"X-Lab-Token": str(token), "Content-Type": "application/json"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        role: Role | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        if role is not None:
            headers = self._headers(role)
        else:
            self._require_configured()
            headers = {"Content-Type": "application/json"}
        try:
            response = await self._client.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                json=json,
                timeout=self._request_timeout,
            )
        except httpx.HTTPError as exc:
            raise VmMasterError(f"vm_master request failed: {exc}") from exc
        if response.status_code == 409:
            raise VmMasterBusyError(self._error_message(response))
        if not response.is_success:
            raise VmMasterError(self._error_message(response))
        try:
            return response.json()
        except ValueError as exc:
            raise VmMasterError("vm_master returned malformed JSON") from exc

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = response.text[:2_000]
        return f"vm_master returned HTTP {response.status_code}: {payload}"

    async def health(self) -> dict[str, str]:
        self._require_configured()
        payload = await self._request("GET", "/healthz")
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise VmMasterError("vm_master health response is not ok")
        return payload

    async def list_scenarios(self) -> list[VmScenarioSummary]:
        payload = await self._request("GET", "/v1/attacks", role="evaluator")
        if not isinstance(payload, list):
            raise VmMasterError("vm_master scenario catalog is malformed")
        return [VmScenarioSummary.model_validate(item) for item in payload]

    async def get_scenario(self, scenario_id: str) -> VmScenario:
        payload = await self._request("GET", f"/v1/attacks/{scenario_id}", role="evaluator")
        return VmScenario.model_validate(payload)

    async def start_job(
        self,
        scenario_id: str,
        action: JobAction,
        *,
        command: str | None = None,
        timeout_seconds: int = 30,
        verification_evidence: dict[str, Any] | None = None,
    ) -> tuple[str, Role]:
        role: Role = "sandbox" if action == "sandbox" else "evaluator"
        body = None
        if action == "sandbox":
            if not command:
                raise ValueError("sandbox job requires a command")
            body = {"command": command, "timeout_seconds": timeout_seconds}
        elif action == "verify":
            body = verification_evidence
        payload = await self._request(
            "POST",
            f"/v1/attacks/{scenario_id}/jobs/{action}",
            role=role,
            json=body,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
            raise VmMasterError("vm_master job creation response is malformed")
        return payload["id"], role

    async def wait_job(
        self,
        job_id: str,
        role: Role,
        *,
        on_log: LogCallback | None = None,
        timeout_seconds: float | None = None,
        allow_command_failure: bool = False,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + (timeout_seconds or self._job_timeout_seconds)
        cursor = 0
        while True:
            payload = await self._request("GET", f"/v1/jobs/{job_id}?after={cursor}", role=role)
            if not isinstance(payload, dict):
                raise VmMasterError("vm_master job response is malformed")
            events = payload.get("events") or []
            if not isinstance(events, list):
                raise VmMasterError("vm_master job events are malformed")
            for event in events:
                if not isinstance(event, dict):
                    continue
                cursor = max(cursor, int(event.get("seq") or 0))
                if on_log is not None:
                    pending = on_log(event)
                    if inspect.isawaitable(pending):
                        await pending
            status = payload.get("status")
            if status == "failed":
                result = payload.get("result")
                # A shell exit status is an observation, not a transport failure.
                if (
                    allow_command_failure
                    and not payload.get("error")
                    and isinstance(result, dict)
                    and isinstance(result.get("exit_code"), int)
                    and all(key in result for key in ("stdout", "stderr", "timed_out"))
                ):
                    return result
                message = payload.get("error") or "vm_master job failed"
                result = payload.get("result")
                if isinstance(result, dict):
                    message = result.get("stderr") or message
                raise VmMasterJobError(str(message), payload)
            if status == "completed":
                result = payload.get("result")
                if not isinstance(result, dict):
                    raise VmMasterError("vm_master completed job has no result")
                return result
            if status != "running":
                raise VmMasterError(f"vm_master returned unknown job status: {status}")
            if time.monotonic() >= deadline:
                raise VmMasterJobError(
                    f"vm_master job {job_id} did not finish before the client timeout",
                    payload,
                )
            await asyncio.sleep(self._job_poll_seconds)

    async def run_job(
        self,
        scenario_id: str,
        action: JobAction,
        *,
        command: str | None = None,
        timeout_seconds: int = 30,
        on_log: LogCallback | None = None,
        job_timeout_seconds: float | None = None,
        wait_if_busy: bool = False,
        verification_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        job_timeout = job_timeout_seconds or self._job_timeout_seconds
        deadline = time.monotonic() + job_timeout
        while True:
            try:
                job_id, role = await self.start_job(
                    scenario_id,
                    action,
                    command=command,
                    timeout_seconds=timeout_seconds,
                    verification_evidence=verification_evidence,
                )
            except VmMasterBusyError:
                if not wait_if_busy or time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(self._job_poll_seconds)
                continue
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise VmMasterJobError("vm_master resource stayed busy until the client timeout")
        return await self.wait_job(
            job_id,
            role,
            on_log=on_log,
            timeout_seconds=remaining,
            allow_command_failure=action == "sandbox",
        )

    async def submit_flag(self, scenario_id: str, flag: str) -> dict[str, Any]:
        payload = await self._request(
            "POST",
            f"/v1/attacks/{scenario_id}/submit",
            role="evaluator",
            json={"flag": flag},
        )
        if not isinstance(payload, dict):
            raise VmMasterError("vm_master flag submission response is malformed")
        return payload
