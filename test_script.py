#!/usr/bin/env python3
"""Последовательный прогон матрицы сценарий × пара моделей через Agent Relay API."""
import json
import os
import time
import urllib.error
import urllib.request
from uuid import uuid4

REASONING_EFFORT = "low"
OPENROUTER_URL = "https://openrouter.ai/api/v1"
OPENROUTER_NITRO = True
ABLITERATION_URL = "https://api.abliteration.ai/v1"

BASE = os.environ.get("AGENT_RELAY_URL", "http://127.0.0.1:8990")
TERMINAL = {"completed", "failed", "aborted", "cleanup_failed"}
POLL_SECONDS, MAX_WAIT_SECONDS = 10, 7200

_REASONING_EFFORT_ORDER = {
    "none": 0,
    "minimal": 1,
    "low": 2,
    "medium": 3,
    "high": 4,
    "xhigh": 5,
    "max": 6,
}
if REASONING_EFFORT not in _REASONING_EFFORT_ORDER:
    raise ValueError(
        f"Invalid REASONING_EFFORT={REASONING_EFFORT!r}; "
        f"choose one of {', '.join(_REASONING_EFFORT_ORDER)}"
    )

SCENARIOS = [
    "docker-socket", "privileged", "pid-namespace", "sys-admin",
    "sys-module", "dac-read-search", "hostpath-etc",
    "context-poisoning", "message-poisoning",
    "privilege-boundary", "tool-misuse",
]

SCENARIOS = [
    "docker-socket",
]


def supported_reasoning_effort(*supported, prefer_higher=False):
    """Use the global effort, clamped to the model's advertised effort ladder."""
    if REASONING_EFFORT in supported:
        return REASONING_EFFORT
    target = _REASONING_EFFORT_ORDER[REASONING_EFFORT]
    return min(
        supported,
        key=lambda effort: (
            abs(_REASONING_EFFORT_ORDER[effort] - target),
            -_REASONING_EFFORT_ORDER[effort]
            if prefer_higher
            else _REASONING_EFFORT_ORDER[effort],
        ),
    )


def MODEL(
    model,
    base_url,
    api_key=None,
    api_key_env=None,
    *,
    max_tokens=16384,
    reasoning_effort=None,
):
    """Build model config with an optional direct Bearer credential.

    api_key_env is resolved in this script's environment and sent as api_key;
    it is not the name of an environment variable inside llm_convo.
    """
    if api_key is not None and api_key_env is not None:
        raise ValueError("Set either api_key or api_key_env, not both")
    if api_key_env is not None:
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise RuntimeError(f"Environment variable {api_key_env} is not set")
    if OPENROUTER_NITRO and base_url.rstrip("/") == OPENROUTER_URL.rstrip("/"):
        model = f"{model}:nitro"
    config = {
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "api_key_env": None,
        "parameters": {"max_tokens": max_tokens},
        # Keep the request valid: output budget must fit below the context limit.
        "context_size": max(32768, max_tokens * 2),
    }
    if reasoning_effort is not None:
        config["reasoning_effort"] = reasoning_effort
    return config


MODELS_POOL_A = [
    # ("qwen", MODEL("huihui-qwen3.5-4b", "http://10.8.8.8:8884/v1",
    #                max_tokens=8192)),
    # ("abliterated-model", MODEL("abliterated-model", ABLITERATION_URL,
    #                             api_key_env="ABLIT_KEY",
    #                             reasoning_effort=supported_reasoning_effort(
    #                                 "none", "minimal", "low", "medium", "high", "xhigh", "max"
    #                             ))),
    # ("abliterated-model-large", MODEL("abliterated-model-large", ABLITERATION_URL,
    #                                   api_key_env="ABLIT_KEY",
    #                                   reasoning_effort=supported_reasoning_effort(
    #                                       "none", "high", "max"
    #                                   ))),
    ("abliterated-model-large-v2", MODEL("abliterated-model-large-v2", ABLITERATION_URL,
                                         api_key_env="ABLIT_KEY",
                                         reasoning_effort=supported_reasoning_effort(
                                             "low", "high", "max"
                                         ))),
    # ("abliterated-model-large-flash", MODEL("abliterated-model-large-flash",
    #                                         ABLITERATION_URL,
    #                                         api_key_env="ABLIT_KEY",
    #                                         reasoning_effort=supported_reasoning_effort(
    #                                             "none", "low", "high", "max"
    #                                         ))),
]
MODELS_POOL_B = [
    # ("qwen", MODEL("Qwen/Qwen3.5-4B", "http://10.8.8.8:8004/v1",
    #                max_tokens=8192)),

    # ("qwen3.5-9b", MODEL("qwen/qwen3.5-9b", OPENROUTER_URL,
    #                     api_key_env="OPENROUTER_API_KEY")),  # 9B
    # ("ministral-14b", MODEL("mistralai/ministral-14b-2512", OPENROUTER_URL,
    #                        api_key_env="OPENROUTER_API_KEY")),  # 14B
    ("gpt-oss-20b", MODEL("openai/gpt-oss-20b", OPENROUTER_URL,
                         api_key_env="OPENROUTER_API_KEY",
                         reasoning_effort=supported_reasoning_effort(
                             "low", "medium", "high"
                         ))),  # 21B
    # ("qwen3.6-35b", MODEL("qwen/qwen3.6-35b-a3b", OPENROUTER_URL,
    #                      api_key_env="OPENROUTER_API_KEY")),  # 35B
    # ("qwen3-coder-next", MODEL("qwen/qwen3-coder-next", OPENROUTER_URL,
    #                           api_key_env="OPENROUTER_API_KEY")),  # 80B
    # ("nemotron-3-super", MODEL("nvidia/nemotron-3-super-120b-a12b", OPENROUTER_URL,
    #                           api_key_env="OPENROUTER_API_KEY",
    #                           reasoning_effort=supported_reasoning_effort(
    #                               "none", "low", "medium"
    #                           ))),  # 120B
    # ("minimax-m2.7", MODEL("minimax/minimax-m2.7", OPENROUTER_URL,
    #                       api_key_env="OPENROUTER_API_KEY")),  # 230B
    # ("mimo-v2.6-flash", MODEL("xiaomi/mimo-v2.6-flash", OPENROUTER_URL,
    #                         api_key_env="OPENROUTER_API_KEY")),  # 309B
    # ("nemotron-3-ultra", MODEL("nvidia/nemotron-3-ultra-550b-a55b", OPENROUTER_URL,
    #                           api_key_env="OPENROUTER_API_KEY",
    #                           reasoning_effort=supported_reasoning_effort(
    #                               "none", "medium", "high"
    #                           ))),  # 550B
    # ("glm-5.3", MODEL("z-ai/glm-5.3", OPENROUTER_URL,
    #                   api_key_env="OPENROUTER_API_KEY",
    #                   reasoning_effort=supported_reasoning_effort(
    #                       "low", "high", "max", prefer_higher=True
    #                   ))),  # 753B
]

PAIRS = [
    (f"{a_name}-a_vs_{b_name}-b", a_cfg, b_cfg)
    for a_name, a_cfg in MODELS_POOL_A
    for b_name, b_cfg in MODELS_POOL_B
]


def call(method, path, body=None, headers=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()[:500]}") from e


def run_one(scenario_id, pair_name, a_cfg, b_cfg):
    scenario = call("GET", f"/v1/benchmark-scenarios/{scenario_id}")
    created = call("POST", "/v1/benchmark-runs", {
        "scenario_id": scenario_id,
        "agent_a": {**a_cfg, "system_prompt": scenario["prompt_a"]},
        "agent_b": {**b_cfg, "system_prompt": scenario["prompt_b"]},
        "max_rounds": 40, "timeout_seconds": 3600,
    }, headers={"Idempotency-Key": f"{scenario_id}:{pair_name}:{uuid4()}"})
    cid, started = created["conversation_id"], time.monotonic()
    while time.monotonic() - started < MAX_WAIT_SECONDS:
        view = call("GET", f"/v1/benchmark-runs/{cid}")
        if view["stage"] in TERMINAL:
            return {"scenario": scenario_id, "pair": pair_name, "conversation_id": cid,
                    "stage": view["stage"], "passed": (view.get("verdict") or {}).get("passed"),
                    "reason": view.get("completion_reason"), "error": view.get("error")}
        time.sleep(POLL_SECONDS)
    return {"scenario": scenario_id, "pair": pair_name, "conversation_id": cid,
            "stage": "script_timeout", "passed": None, "reason": None, "error": "poll timeout"}

def main():
    results = []
    for scenario_id in SCENARIOS:            # внешний цикл — сценарии
        for pair_name, a_cfg, b_cfg in PAIRS:  # внутренний — пары моделей
            print(f"==> {scenario_id} × {pair_name}", flush=True)
            result = run_one(scenario_id, pair_name, a_cfg, b_cfg)
            results.append(result)
            print(f"    {result['stage']} passed={result['passed']} "
                  f"reason={result['reason']} error={result['error']}", flush=True)

    with open("benchmark_results.json", "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"Done: {sum(r['passed'] is True for r in results)}/{len(results)} passed "
          f"-> benchmark_results.json")


if __name__ == "__main__":
    main()
