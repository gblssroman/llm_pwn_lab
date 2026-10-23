import base64
import os
import shlex
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from lab_service.models import ExecRequest, ExecResult
from lab_service.runner import Runner, encoded_shell
from lab_service.scenarios import Scenarios


@pytest.mark.parametrize(
    "command",
    [
        "id",
        "echo '$(touch /tmp/NOPE)' ; echo a",
        'printf "%s" "привет"',
        "echo one\necho two",
    ],
)
def test_base64_roundtrip(command):
    wrapped = encoded_shell(command)
    assert base64.b64encode(command.encode()).decode() in wrapped
    assert command not in wrapped
    result = subprocess.run(["sh", "-c", wrapped], capture_output=True)
    expected = subprocess.run(["sh", "-c", command], capture_output=True)
    assert (result.returncode, result.stdout) == (expected.returncode, expected.stdout)


def test_sandbox_wrapper(settings, attack, monkeypatch):
    runner = Runner(settings)
    remote_call = Mock(return_value=ExecResult(exit_code=0))
    monkeypatch.setattr(runner, "remote", remote_call)
    command = "echo 'hello'; $(printf harmless)"
    runner.sandbox(attack, ExecRequest(command=command))
    remote = remote_call.call_args.args[1]
    assert command not in remote
    argv = shlex.split(remote)
    assert argv[:12] == [
        "cd",
        "/workspace",
        "&&",
        "sudo",
        "docker",
        "compose",
        "-f",
        "vm/agent-compose.yml",
        "exec",
        "-T",
        "sandbox",
        "timeout",
    ]
    assert argv[-1] == encoded_shell(command)


def test_escape_sandbox_uses_guest_copy(monkeypatch):
    from conftest import ROOT

    from lab_service.config import Settings

    settings = Settings(root=ROOT, sandbox_token="s" * 32, evaluator_token="e" * 32)
    attack = Scenarios(ROOT, "escapes").load("docker-socket")
    runner = Runner(settings)
    remote_call = Mock(return_value=ExecResult(exit_code=0))
    monkeypatch.setattr(runner, "remote", remote_call)
    runner.sandbox(attack, ExecRequest(command="id"))
    assert remote_call.call_args.args[1].startswith(
        "cd /opt/agentic-lab && sudo docker compose"
    )


def test_direct_ssh_config_is_cached_per_mode(settings, attack, monkeypatch):
    identity = settings.root / "private_key"
    identity.write_text("test")
    runner = Runner(settings)
    config = ExecResult(
        exit_code=0,
        stdout=(
            "Host default\n"
            "  HostName 127.0.0.1\n"
            "  User vagrant\n"
            "  Port 2222\n"
            f"  IdentityFile {identity}\n"
        ),
    )
    vagrant = Mock(return_value=config)
    monkeypatch.setattr(runner, "run", vagrant)
    process = Mock(pid=123, returncode=0)
    process.communicate.return_value = (b"ok", b"")
    popen = Mock(return_value=process)
    monkeypatch.setattr(subprocess, "Popen", popen)

    runner.remote(attack, "id", 30)
    runner.remote(attack, "true", 30)
    escape = attack.model_copy(
        update={
            "mode": "escape",
            "profile": "docker-socket",
            "setup_script": "vm/escape/setup.sh",
        }
    )
    runner.remote(escape, "id", 30)

    assert vagrant.call_count == 2
    assert vagrant.call_args.args == (escape, ["ssh-config"], 30)
    assert popen.call_count == 3
    argv = popen.call_args.args[0]
    assert argv[-3:] == ["--", "vagrant@127.0.0.1", "id"]
    assert "ControlMaster=auto" in argv


def test_restored_vm_uses_a_new_controlmaster(settings, attack, monkeypatch):
    identity = settings.root / "private_key"
    identity.write_text("test")
    runner = Runner(settings)
    config = ExecResult(
        exit_code=0,
        stdout=(
            "Host default\n"
            "  HostName 127.0.0.1\n"
            "  User vagrant\n"
            "  Port 2222\n"
            f"  IdentityFile {identity}\n"
        ),
    )
    vagrant = Mock(return_value=config)
    monkeypatch.setattr(runner, "run", vagrant)
    process = Mock(pid=123, returncode=0)
    process.communicate.return_value = (b"ok", b"")
    popen = Mock(return_value=process)
    monkeypatch.setattr(subprocess, "Popen", popen)

    runner.remote(attack, "id", 30)
    first = next(
        item
        for item in popen.call_args.args[0]
        if item.startswith("ControlPath=")
    )
    path, _ = runner.paths(attack)
    runner._invalidate_ssh(path, attack.mode, attack.id)
    runner.remote(attack, "id", 30)
    second = next(
        item
        for item in popen.call_args.args[0]
        if item.startswith("ControlPath=")
    )

    assert first != second
    assert vagrant.call_count == 2


def test_lifecycle_paths_and_environment(settings, attack, monkeypatch):
    process = Mock(pid=123, returncode=0)
    process.communicate.return_value = (b"ok", b"")
    popen = Mock(return_value=process)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setenv("LAB_EVALUATOR_TOKEN", "do-not-forward")
    runner = Runner(settings)
    remote = Mock(return_value=ExecResult(exit_code=0))
    monkeypatch.setattr(runner, "remote", remote)
    runner.up(attack)
    kwargs = popen.call_args.kwargs
    assert kwargs["env"]["VAGRANT_VAGRANTFILE"] == "Vagrantfile"
    assert kwargs["env"]["VAGRANT_DOTFILE_PATH"] == str(
        settings.root
        / ".runtime/vagrant/scenarios/20261023-000000-000000/agent/context-poisoning"
    )
    assert kwargs["cwd"] == settings.root / "vagrant"
    assert kwargs["env"]["LAB_SETUP_SCRIPT"] == str(settings.root / "vm/setup.sh")
    assert kwargs["env"]["LAB_LINKED_CLONE"] == "1"
    assert "LAB_EVALUATOR_TOKEN" not in kwargs["env"]
    assert remote.call_args.args[1] == "sudo bash /workspace/vm/activate.sh"
    runner.down(attack)
    assert popen.call_args.args[0] == [
        "vagrant",
        "snapshot",
        "restore",
        "agentic-lab-clean",
        "--no-start",
    ]


def test_agent_and_escape_use_separate_vagrant_state(settings, attack, monkeypatch):
    process = Mock(pid=123, returncode=0)
    process.communicate.return_value = (b"ok", b"")
    popen = Mock(return_value=process)
    monkeypatch.setattr(subprocess, "Popen", popen)
    runner = Runner(settings)
    escape = attack.model_copy(
        update={
            "mode": "escape",
            "profile": "docker-socket",
            "setup_script": "vm/escape/setup.sh",
        }
    )

    runner.run(attack, ["status"], 30)
    agent_state = popen.call_args.kwargs["env"]["VAGRANT_DOTFILE_PATH"]
    runner.run(escape, ["status"], 30)
    escape_state = popen.call_args.kwargs["env"]["VAGRANT_DOTFILE_PATH"]

    base = settings.root / ".runtime/vagrant/scenarios/20261023-000000-000000"
    assert agent_state == str(base / "agent/context-poisoning")
    assert escape_state == str(base / "escape/context-poisoning")
    assert agent_state != escape_state


def test_timeout_kills_process_group(settings, attack, monkeypatch):
    process = Mock(pid=123)
    process.communicate.side_effect = [
        subprocess.TimeoutExpired("vagrant", 1),
        (b"partial", b""),
    ]
    monkeypatch.setattr(subprocess, "Popen", Mock(return_value=process))
    kill = Mock()
    monkeypatch.setattr(os, "killpg", kill)
    result = Runner(settings).run(attack, ["up"], 1)
    assert result.timed_out and result.exit_code == 124
    kill.assert_called_once_with(123, signal.SIGKILL)
    assert process.communicate.call_count == 2


def test_shared_lifecycle_lock(settings, attack, monkeypatch):
    first, second = Runner(settings), Runner(settings)
    other = attack.model_copy(
        update={
            "mode": "escape",
            "profile": "docker-socket",
            "setup_script": "vm/escape/setup.sh",
        }
    )
    monkeypatch.setattr("lab_service.runner.scenario_snapshot", lambda *_: "clean")
    assert first.lock_for(attack) is second.lock_for(other)
    active, maximum = 0, 0
    guard = threading.Lock()

    def run(*args):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.03)
        with guard:
            active -= 1
        return ExecResult(exit_code=0)

    monkeypatch.setattr(first, "run", run)
    monkeypatch.setattr(first, "remote", run)
    monkeypatch.setattr(second, "run", run)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(first.up, attack), pool.submit(second.down, other)]
        for future in futures:
            future.result()
    assert maximum == 1
