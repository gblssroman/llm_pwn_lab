import base64
import json
import shlex
from contextlib import nullcontext
from unittest.mock import Mock

import pytest

from lab_service.config import Settings
from lab_service.escape_diagnostics import evaluate
from lab_service.models import ExecResult
from lab_service.runner import Runner
from lab_service.scenarios import Scenarios

from conftest import ROOT


@pytest.mark.parametrize("missing", [None, "cap_eff", "seccomp", "handles", "security"])
def test_preflight_distinguishes_missing_prerequisites(missing):
    runtime = {"uid": "0", "cap_eff": "0000000000000004", "seccomp": "0"}
    options = ["seccomp:unconfined"]
    handles = {"supported": True}
    if missing in {"cap_eff", "seccomp"}:
        runtime[missing] = "0" if missing == "cap_eff" else "2"
    elif missing == "handles":
        handles = {"supported": False, "errno": "EOPNOTSUPP"}
    elif missing == "security":
        options = []
    report = evaluate(runtime, options, handles)
    assert report["status"] == ("ready" if missing is None else "failed")
    assert report["escape_viability"] == "not_tested"


@pytest.mark.parametrize("exit_code", [0, 1])
def test_runner_preflights_after_activation_without_modifying_snapshot(monkeypatch, exit_code):
    settings = Settings(root=ROOT, sandbox_token="s" * 32, evaluator_token="e" * 32)
    attack = Scenarios(ROOT, "escapes").load("dac-read-search")
    runner = Runner(settings)
    monkeypatch.setattr(runner, "transaction", lambda _: nullcontext())
    monkeypatch.setattr("lab_service.runner.scenario_snapshot", lambda *_: "clean")
    monkeypatch.setattr(runner, "run", Mock(return_value=ExecResult(exit_code=0)))
    report = {"version": 1, "status": "ready" if exit_code == 0 else "failed"}
    remote = Mock(side_effect=[
        ExecResult(exit_code=0, stdout="ready"),
        ExecResult(exit_code=exit_code, stdout="[lab] escape preflight: " + json.dumps(report)),
    ])
    monkeypatch.setattr(runner, "remote", remote)
    result = runner.up(attack)
    assert result.exit_code == exit_code
    assert "escape preflight" in result.stdout
    activation, diagnostic = remote.call_args_list
    assert "activate.sh" in activation.args[1]
    argv = shlex.split(diagnostic.args[1])
    assert argv[:3] == ["sudo", "python3", "-c"]
    payload = argv[3].split("base64.b64decode('", 1)[1].split("'", 1)[0]
    source = base64.b64decode(payload).decode()
    compile(source, "preflight", "exec")
    assert "escape-flag.txt" not in source
    assert diagnostic.args[2] == 60


def test_failed_activation_does_not_run_preflight(monkeypatch):
    runner = Runner(Settings(root=ROOT, sandbox_token="s" * 32, evaluator_token="e" * 32))
    attack = Scenarios(ROOT, "escapes").load("dac-read-search")
    monkeypatch.setattr(runner, "transaction", lambda _: nullcontext())
    monkeypatch.setattr("lab_service.runner.scenario_snapshot", lambda *_: "clean")
    monkeypatch.setattr(runner, "run", Mock(return_value=ExecResult(exit_code=0)))
    remote = Mock(return_value=ExecResult(exit_code=1))
    monkeypatch.setattr(runner, "remote", remote)
    assert runner.up(attack).exit_code == 1
    assert remote.call_count == 1
