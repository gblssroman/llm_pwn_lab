import shutil

from conftest import ROOT, FakeRunner
from fastapi.testclient import TestClient
from lab_service.app import create_app
from lab_service.config import Settings
from lab_service.models import ExecRequest, ExecResult
from lab_service.runner import Runner
from lab_service.scenarios import Scenarios


def escape_client(tmp_path):
    for directory in ("attacks", "escapes", "vm/escape/profiles"):
        shutil.copytree(ROOT / directory, tmp_path / directory)
    for name in ("vagrant/Vagrantfile", "vm/setup.sh", "vm/escape/setup.sh"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    settings = Settings(root=tmp_path, sandbox_token="s" * 32, evaluator_token="e" * 32)
    runner = FakeRunner()
    runner.result = ExecResult(exit_code=0, stdout="flag{correct}\n")
    return TestClient(create_app(settings, runner)), runner


def test_escape_catalog_and_flag_submission(tmp_path):
    client, runner = escape_client(tmp_path)
    with client:
        auth = {"X-Lab-Token": "e" * 32}
        items = client.get("/v1/attacks", headers=auth).json()
        assert len(items) == 12
        assert sum(item["mode"] == "escape" for item in items) == 7
        url = "/v1/attacks/docker-socket"
        scenario = client.get(url, headers=auth).json()
        assert scenario["prompt_a"]
        assert scenario["prompt_b"]
        assert "prompt" not in scenario
        assert scenario["metadata"]["hint"]
        assert scenario["profile_config"]["services"]["sandbox"]["volumes"] == [
            "/var/run/docker.sock:/var/run/docker.sock"
        ]
        assert (
            client.post(
                url + "/submit",
                json={"flag": "flag{correct}"},
                headers={"X-Lab-Token": "s" * 32},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/attacks/context-poisoning/submit",
                json={"flag": "flag{correct}"},
                headers=auth,
            ).status_code
            == 422
        )
        assert not client.post(url + "/verify", headers=auth).json()["passed"]
        assert not client.post(
            url + "/submit", json={"flag": "flag{wrong}"}, headers=auth
        ).json()["passed"]
        assert client.post(
            url + "/submit", json={"flag": "flag{correct}"}, headers=auth
        ).json()["passed"]
        assert client.post(url + "/verify", headers=auth).json()["passed"]
        assert client.post("/v1/attacks/privileged/up", headers=auth).status_code == 200
        assert not client.post(url + "/verify", headers=auth).json()["passed"]
        assert client.post(url + "/down", headers=auth).status_code == 200
        assert any(
            call[0] == "evaluator" and "escape-flag.txt" in call[2].command
            for call in runner.calls
        )


def test_escape_sandbox_uses_selected_compose_profile(monkeypatch):
    scenario = Scenarios(ROOT, "escapes").load("docker-socket")
    settings = Settings(root=ROOT, sandbox_token="s" * 32, evaluator_token="e" * 32)
    runner = Runner(settings)
    called = []

    def fake_remote(attack, command, timeout):
        called.append((command, timeout))
        return ExecResult(exit_code=0)

    monkeypatch.setattr(runner, "remote", fake_remote)
    runner.sandbox(scenario, ExecRequest(command="id"))
    assert "vm/escape/compose.yml" in called[0][0]
    assert "vm/escape/profiles/docker-socket.yml" in called[0][0]
    assert called[0][1] == 45
