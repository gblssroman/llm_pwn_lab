"""Send and verify a small observation against the configured self-hosted Langfuse."""

import time
import uuid

import httpx
from langfuse import Langfuse, propagate_attributes

from llm_convo.config import get_settings


def main() -> None:
    settings = get_settings()
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        raise SystemExit("Langfuse credentials are missing; run ./scripts/init-langfuse.sh")

    base_url = settings.langfuse_base_url or "http://localhost:8992"
    health = httpx.get(
        f"{base_url.rstrip('/')}/api/public/health?failIfDatabaseUnavailable=true",
        timeout=10,
    )
    health.raise_for_status()

    client = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        base_url=base_url,
        environment=settings.langfuse_tracing_environment,
    )
    seed = f"llm-convo-smoke-{uuid.uuid4()}"
    trace_id = client.create_trace_id(seed=seed)
    with client.start_as_current_observation(
        trace_context={"trace_id": trace_id},
        name="llm-convo-self-hosted-smoke",
        as_type="agent",
        input={"check": "self-hosted Langfuse connection"},
    ) as root:
        with propagate_attributes(
            session_id="langfuse-smoke",
            trace_name="llm-convo-self-hosted-smoke",
            tags=["smoke-test"],
        ):
            with client.start_as_current_observation(
                name="smoke-generation",
                as_type="generation",
                model="smoke-model",
                input=[{"role": "user", "content": "ping"}],
            ) as generation:
                generation.update(
                    output="pong",
                    usage_details={"input": 1, "output": 1, "total": 2},
                )
        root.update(output={"status": "ok"})
    client.flush()

    deadline = time.monotonic() + 30
    found = 0
    while time.monotonic() < deadline:
        response = client.api.observations.get_many(trace_id=trace_id, limit=10)
        found = len(response.data)
        if found >= 2:
            break
        time.sleep(1)
    client.shutdown()

    if found < 2:
        raise SystemExit(
            f"Trace {trace_id} was sent but was not queryable within 30 seconds"
        )
    print(f"Langfuse connection OK: trace_id={trace_id}, observations={found}")
    print(f"Open UI: {base_url}")


if __name__ == "__main__":
    main()
