#!/usr/bin/env python3
"""Последовательный прогон матрицы сценарий × пара моделей через Agent Relay API."""
import json
import os
import time
import urllib.error
import urllib.request
from uuid import uuid4

BASE = os.environ.get("AGENT_RELAY_URL", "http://127.0.0.1:8990")
TERMINAL = {"completed", "failed", "aborted", "cleanup_failed"}
POLL_SECONDS, MAX_WAIT_SECONDS = 10, 7200

SCENARIOS = [
    "docker-socket", "privileged", "pid-namespace", "sys-admin",
    "sys-module", "dac-read-search", "hostpath-etc",
    "context-poisoning", "memory-poisoning", "message-poisoning",
    "privilege-boundary", "tool-misuse",
]


def MODEL(model, base_url, api_key=None, api_key_env=None):
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
    return {
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "api_key_env": None,
    }


OPENROUTER_URL = "https://openrouter.ai/api/v1"
MODELS_POOL_A = [
    ("qwen", MODEL("huihui-qwen3.5-4b", "http://localhost:8884/v1")),
    ("kimi-k3.1-pro", MODEL("moonshotai/kimi-k3.1-pro", OPENROUTER_URL,
                            api_key_env="OPENROUTER_API_KEY")),
    ("minimax-m3.1-flash", MODEL("minimax/minimax-m3.1-flash", OPENROUTER_URL,
                                 api_key_env="OPENROUTER_API_KEY")),
    ("deepseek-v4.1-flash", MODEL("deepseek/deepseek-v4.1-flash-1017", OPENROUTER_URL,
                                  api_key_env="OPENROUTER_API_KEY")),
    ("beam-1.0", MODEL("reflection/beam-1.0-501b-a23b", OPENROUTER_URL,
                       api_key_env="OPENROUTER_API_KEY")),
    ("mercury-3", MODEL("inception/mercury-3", OPENROUTER_URL,
                        api_key_env="OPENROUTER_API_KEY")),
    ("muse-spark-open", MODEL("meta/muse-spark-open", OPENROUTER_URL,
                              api_key_env="OPENROUTER_API_KEY")),
    ("nemotron-3.5-super-vl-free", MODEL("nvidia/nemotron-3.5-super-vl:free",
                                         OPENROUTER_URL,
                                         api_key_env="OPENROUTER_API_KEY")),
    ("pareto-26.10", MODEL("unbiased/pareto-26.10", OPENROUTER_URL,
                           api_key_env="OPENROUTER_API_KEY")),
    ("lfm-2.5-24b-a2b", MODEL("liquid/lfm-2.5-24b-a2b", OPENROUTER_URL,
                              api_key_env="OPENROUTER_API_KEY")),
    ("gemini-4-argon", MODEL("google/gemini-4-argon", OPENROUTER_URL,
                             api_key_env="OPENROUTER_API_KEY")),
]
MODELS_POOL_B = [
    ("qwen", MODEL("Qwen/Qwen3.5-4B", "http://localhost:8004/v1")),
    ("kimi-k3.1-pro", MODEL("moonshotai/kimi-k3.1-pro", OPENROUTER_URL,
                            api_key_env="OPENROUTER_API_KEY")),
    ("minimax-m3.1-flash", MODEL("minimax/minimax-m3.1-flash", OPENROUTER_URL,
                                 api_key_env="OPENROUTER_API_KEY")),
    ("deepseek-v4.1-flash", MODEL("deepseek/deepseek-v4.1-flash-1017", OPENROUTER_URL,
                                  api_key_env="OPENROUTER_API_KEY")),
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
