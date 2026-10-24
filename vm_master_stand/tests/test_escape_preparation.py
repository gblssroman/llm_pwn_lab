import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from lab_service.config import Settings
from lab_service.models import ExecResult
from lab_service.runner import Runner
from lab_service.scenarios import Scenarios

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("failure", [None, "exit", "timeout"])
def test_escape_up_restores_snapshot_and_stops_on_failed_restore(monkeypatch, failure):
    runner = Runner(
        Settings(root=ROOT, sandbox_token="s" * 32, evaluator_token="e" * 32)
    )
    # Keep lifecycle lock files out of the real repository.
    from contextlib import nullcontext

    monkeypatch.setattr(runner, "transaction", lambda _: nullcontext())
    calls = []

    monkeypatch.setattr("lab_service.runner.scenario_snapshot", lambda *_: "clean")

    def run(attack, args, timeout):
        calls.append(args)
        if args[:2] == ["snapshot", "restore"] and failure:
            return ExecResult(
                exit_code=1 if failure == "exit" else 124,
                timed_out=failure == "timeout",
            )
        return ExecResult(exit_code=0)

    monkeypatch.setattr(runner, "run", run)
    monkeypatch.setattr(
        "lab_service.runner.secrets.token_hex", lambda size: "a" * (size * 2)
    )
    remote = Mock(return_value=ExecResult(exit_code=0))
    monkeypatch.setattr(runner, "remote", remote)
    result = runner.up(Scenarios(ROOT, "escapes").load("docker-socket"))
    assert calls[0] == ["snapshot", "restore", "clean"]
    if failure:
        assert len(calls) == 1 and result.exit_code
        assert not remote.called
    else:
        assert len(calls) == 1
        assert "prepared VM restore + activation:" in result.stdout
        assert "LAB_ESCAPE_PROFILE=docker-socket" in remote.call_args.args[1]
        assert (
            "LAB_ESCAPE_FLAG='flag{" + "a" * 40 + "}'"
            in remote.call_args.args[1]
        )


def test_escape_down_restores_snapshot_without_start(monkeypatch):
    runner = Runner(
        Settings(root=ROOT, sandbox_token="s" * 32, evaluator_token="e" * 32)
    )
    from contextlib import nullcontext

    monkeypatch.setattr(runner, "transaction", lambda _: nullcontext())
    monkeypatch.setattr("lab_service.runner.scenario_snapshot", lambda *_: "clean")
    run = Mock(return_value=ExecResult(exit_code=0))
    monkeypatch.setattr(runner, "run", run)
    runner.down(Scenarios(ROOT, "escapes").load("docker-socket"))
    assert run.call_args.args[1] == ["snapshot", "restore", "clean", "--no-start"]


def image_attempt(tmp_path, *, cached, version, adapter):
    dockerfile = tmp_path / "Dockerfile"
    adapter_file = tmp_path / "adapter.py"
    log = tmp_path / "docker.log"
    dockerfile.write_text("FROM node:24-bookworm-slim\n")
    adapter_file.write_text(adapter)
    script = """
set -euo pipefail
source "$COMMON"
cat() { command cat "$TEST_DOCKERFILE" "$TEST_ADAPTER"; }
docker() {
  printf '%s %s\\n' "$LAB_ESCAPE_IMAGE" "$*" >> "$TEST_LOG"
  if [[ $1 == image ]]; then return "$CACHE_MISS"; fi
}
lab_image
"""
    env = {
        **os.environ,
        "COMMON": str(ROOT / "vm/escape/common.sh"),
        "TEST_DOCKERFILE": str(dockerfile),
        "TEST_ADAPTER": str(adapter_file),
        "TEST_LOG": str(log),
        "CACHE_MISS": "0" if cached else "1",
        "LAB_OPENCLAW_VERSION": version,
    }
    subprocess.run(
        ["bash", "-c", script], env=env, check=True, capture_output=True, text=True
    )
    return log.read_text().splitlines()


def test_prepared_image_reuse_and_invalidation(tmp_path):
    paths = []
    for i in range(4):
        path = tmp_path / str(i)
        path.mkdir()
        paths.append(path)
    hit = image_attempt(paths[0], cached=True, version="2026.9.6", adapter="old")
    miss = image_attempt(paths[1], cached=False, version="2026.9.6", adapter="old")
    changed_source = image_attempt(
        paths[2], cached=True, version="2026.9.6", adapter="new"
    )
    changed_version = image_attempt(
        paths[3], cached=True, version="2026.9.7", adapter="old"
    )
    assert len(hit) == 1  # Warm path cannot execute a build.
    assert len(miss) == 2 and miss[1].endswith(
        "compose -f vm/escape/compose.yml build sandbox"
    )
    assert hit[0].split()[0] == miss[0].split()[0]
    assert (
        len({rows[0].split()[0] for rows in (hit, changed_source, changed_version)})
        == 3
    )


def test_escape_activation_requires_host_generated_flag():
    script = (ROOT / "vm/escape/activate.sh").read_text()
    assert "LAB_ESCAPE_FLAG:?" in script
    assert "openssl rand" not in script


def builder_module():
    from lab_service import benchmark

    return benchmark


@pytest.mark.parametrize("fail_step", [None, "up", "reload", "ssh", "package"])
def test_builder_isolated_from_benchmark_and_cleans_up(
    monkeypatch, tmp_path, fail_step
):
    module = builder_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setenv("LAB_EVALUATOR_TOKEN", "must-not-reach-builder")
    monkeypatch.setenv("LAB_OPENCLAW_COMMAND", "must-not-configure-a-model")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[1] == fail_step:
            raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(module.subprocess, "run", run)
    output = tmp_path / "prepared.box"
    if fail_step:
        with pytest.raises(subprocess.CalledProcessError):
            module.build_box("bento/ubuntu-24.04", "lab/prepared", output, "2026.9.6")
    else:
        module.build_box("bento/ubuntu-24.04", "lab/prepared", output, "2026.9.6")
        assert calls[-2][0][1:3] == ["box", "add"]
    assert calls[-1][0] == ["vagrant", "destroy", "-f"]
    if not fail_step:
        assert [args[1] for args, _ in calls] == [
            "up",
            "reload",
            "ssh",
            "package",
            "box",
            "destroy",
        ]
    for _, kwargs in calls:
        env = kwargs["env"]
        assert kwargs["cwd"] != tmp_path / "vagrant"
        assert kwargs["cwd"].parent == tmp_path / ".cache/escape-boxes"
        assert env["VAGRANT_CWD"] == str(kwargs["cwd"])
        assert env["LAB_SETUP_SCRIPT"].endswith("vm/escape/prepare-box.sh")
        assert env["LAB_BUILD_ESCAPE_BOX"] == "1"
        assert "LAB_EVALUATOR_TOKEN" not in env
        assert env["LAB_OPENCLAW_COMMAND"] == ""


def test_builder_preserves_existing_output(monkeypatch, tmp_path):
    module = builder_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    output = tmp_path / "prepared.box"
    output.write_bytes(b"existing box")
    with pytest.raises(ValueError, match="overwrite"):
        module.build_box("base", "lab/prepared", output, "2026.9.6")
    assert output.read_bytes() == b"existing box"
    assert not (tmp_path / ".cache").exists()


def test_builder_can_replace_obsolete_kernel_headers(tmp_path):
    log = tmp_path / "apt.log"
    script = """
set -euo pipefail
source "$COMMON"
uname() { printf 'obsolete-test-kernel'; }
docker() { return 0; }
systemctl() { return 0; }
command() {
  if [[ $1 == -v ]]; then return 0; fi
  builtin command "$@"
}
apt-get() { printf '%s\\n' "$*" >> "$TEST_LOG"; }
lab_dependencies all
"""
    subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "COMMON": str(ROOT / "vm/escape/common.sh"),
            "TEST_LOG": str(log),
        },
        capture_output=True,
        check=True,
    )
    installed = log.read_text()
    assert "linux-image-generic linux-headers-generic" in installed
    assert "linux-headers-obsolete-test-kernel" not in installed


def init_root(tmp_path):
    shutil.copytree(ROOT / "attacks", tmp_path / "attacks")
    shutil.copytree(ROOT / "escapes", tmp_path / "escapes")
    for name in builder_module().BOX_INPUTS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, path)
    return tmp_path


def test_init_reuses_registered_box_and_rebuilds_changed_inputs(monkeypatch, tmp_path):
    module = builder_module()
    root = init_root(tmp_path)
    calls = []
    monkeypatch.setattr(module, "box_registered", lambda _: True)
    monkeypatch.setattr(
        module, "build_box", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    snapshots = []
    monkeypatch.setattr(
        module, "prepare_scenario_snapshot", lambda *args: snapshots.append(args)
    )
    monkeypatch.setattr(module, "_scenario_snapshot_available", lambda *args: True)
    first = module.init_benchmark(root, name="lab/prepared")
    assert module.init_benchmark(root) == first
    assert len(calls) == 1
    assert len(snapshots) == 12
    assert first["snapshot"] == "agentic-lab-clean"
    assert len(first["scenarios"]) == 12
    assert module.escape_config(root) == ("lab/prepared", "2026.9.6")
    assert module.escape_config(root, "lab/override", "2026.9.7") == (
        "lab/override",
        "2026.9.7",
    )
    (root / "vm/openclaw-adapter/adapter.py").write_text("changed adapter")
    second = module.init_benchmark(root, name="lab/new")
    assert len(calls) == 2
    assert second["fingerprint"] != first["fingerprint"]
    assert module.selected_box(root) == "lab/new"


def test_ssh_port_migration_reuses_box_but_rebuilds_scenario_clones(
    monkeypatch, tmp_path
):
    module = builder_module()
    root = init_root(tmp_path)
    builds = []
    snapshots = []
    monkeypatch.setattr(module, "box_registered", lambda _: True)
    monkeypatch.setattr(
        module, "build_box", lambda *args, **kwargs: builds.append((args, kwargs))
    )
    monkeypatch.setattr(
        module, "prepare_scenario_snapshot", lambda *args: snapshots.append(args)
    )
    monkeypatch.setattr(module, "_scenario_snapshot_available", lambda *args: True)

    first = module.init_benchmark(root, name="lab/prepared")
    previous = dict(first)
    previous["fingerprint"] = module._box_fingerprint(
        root,
        previous["base_box"],
        previous["openclaw_version"],
        legacy_ssh_config=True,
    )
    (root / ".runtime/benchmark.json").write_text(json.dumps(previous))
    snapshots.clear()
    builds.clear()

    migrated = module.init_benchmark(root)

    assert builds == []
    assert len(snapshots) == 12
    assert migrated["generation"] != first["generation"]
    assert migrated["fingerprint"] == module._box_fingerprint(
        root, migrated["base_box"], migrated["openclaw_version"]
    )


def test_scenario_ssh_ports_are_stable_unique_and_non_ephemeral(tmp_path):
    module = builder_module()
    root = init_root(tmp_path)
    scenarios = [*Scenarios(root).all(), *Scenarios(root, "escapes").all()]
    ports = [module.scenario_ssh_port(item) for item in scenarios]

    module._validate_scenario_ssh_ports(scenarios)

    assert len(set(ports)) == len(scenarios)
    assert all(20000 <= port < 30000 for port in ports)
    docker_socket = Scenarios(root, "escapes").load("docker-socket")
    assert module.scenario_ssh_port(docker_socket) == 26985


def test_scenario_snapshot_is_linked_isolated_and_saved(monkeypatch, tmp_path):
    module = builder_module()
    root = init_root(tmp_path)
    calls = []

    def vagrant(root_arg, environment, *args, **kwargs):
        calls.append((root_arg, environment, list(args), kwargs))

    monkeypatch.setattr(module, "_runtime_vagrant", vagrant)
    attack = Scenarios(root, "escapes").load("docker-socket")
    generation = "20261023-000000-000000"
    module.prepare_scenario_snapshot(
        root, attack, "lab/prepared", "2026.9.7", generation, "clean"
    )

    assert [item[2] for item in calls] == [
        ["destroy", "-f"],
        ["up", "--provider=virtualbox", "--provision"],
        [
            "ssh",
            "-c",
            (
                "cd /opt/agentic-lab && sudo docker info >/dev/null && "
                "sudo docker compose -f vm/escape/compose.yml ps --status running "
                "--services | grep -qx sandbox"
            ),
        ],
        ["snapshot", "save", "-f", "clean"],
        ["snapshot", "restore", "clean", "--no-start"],
    ]
    environment = calls[0][1]
    assert environment["LAB_ISOLATED_ESCAPE"] == "1"
    assert environment["LAB_VAGRANT_BOX"] == "lab/prepared"
    assert environment["LAB_OPENCLAW_VERSION"] == "2026.9.7"
    assert environment["LAB_LINKED_CLONE"] == "1"
    assert environment["LAB_SSH_HOST_PORT"] == "26985"
    assert environment["VAGRANT_DOTFILE_PATH"] == str(
        root
        / ".runtime/vagrant/scenarios/20261023-000000-000000/escape/docker-socket"
    )


def test_scenario_snapshot_retries_only_failed_clone(monkeypatch, tmp_path):
    module = builder_module()
    root = init_root(tmp_path)
    calls = []
    powered_off = []
    failed_once = False

    def vagrant(root_arg, environment, *args, **kwargs):
        nonlocal failed_once
        calls.append(list(args))
        if args[0] == "up" and not failed_once:
            failed_once = True
            raise subprocess.CalledProcessError(1, ["vagrant", *args])

    monkeypatch.setattr(module, "_runtime_vagrant", vagrant)
    monkeypatch.setattr(
        module,
        "_power_off_failed_clone",
        lambda dotfile, attack: powered_off.append(attack.id),
    )
    attack = Scenarios(root, "escapes").load("docker-socket")
    module.prepare_scenario_snapshot(
        root,
        attack,
        "lab/prepared",
        "2026.9.7",
        "20261023-000000-000000",
        "clean",
    )

    assert [call[0] for call in calls].count("up") == 2
    assert [call[0] for call in calls].count("destroy") == 2
    assert powered_off == ["docker-socket"]
    assert calls[-2:] == [
        ["snapshot", "save", "-f", "clean"],
        ["snapshot", "restore", "clean", "--no-start"],
    ]


def test_failed_clone_poweroff_targets_recorded_running_vm(monkeypatch, tmp_path):
    module = builder_module()
    dotfile = tmp_path / "vagrant-state"
    machine = dotfile / "machines/default/virtualbox"
    machine.mkdir(parents=True)
    vm_id = "01234567-89ab-cdef-0123-456789abcdef"
    (machine / "id").write_text(vm_id)
    results = [
        Mock(returncode=0, stdout='VMState="running"\n'),
        Mock(returncode=0, stdout=""),
    ]
    run = Mock(side_effect=results)
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/bin/VBoxManage")

    attack = Scenarios(ROOT, "escapes").load("docker-socket")
    module._power_off_failed_clone(dotfile, attack)

    assert run.call_args_list[0].args[0] == [
        "/usr/bin/VBoxManage",
        "showvminfo",
        vm_id,
        "--machinereadable",
    ]
    assert run.call_args_list[1].args[0] == [
        "/usr/bin/VBoxManage",
        "controlvm",
        vm_id,
        "poweroff",
    ]


def test_init_resumes_staging_generation(monkeypatch, tmp_path):
    module = builder_module()
    root = init_root(tmp_path)
    builds = []
    prepared = []
    ready = set()
    should_fail = True

    monkeypatch.setattr(module, "box_registered", lambda _: True)
    monkeypatch.setattr(
        module, "build_box", lambda *args, **kwargs: builds.append((args, kwargs))
    )

    def available(root_arg, attack, box, version, generation, snapshot):
        return module.scenario_key(attack) in ready

    def prepare(root_arg, attack, box, version, generation, snapshot):
        nonlocal should_fail
        key = module.scenario_key(attack)
        prepared.append(key)
        if should_fail and len(prepared) == 2:
            raise subprocess.CalledProcessError(1, ["vagrant", "up"])
        ready.add(key)

    monkeypatch.setattr(module, "_scenario_snapshot_available", available)
    monkeypatch.setattr(module, "prepare_scenario_snapshot", prepare)

    with pytest.raises(subprocess.CalledProcessError):
        module.init_benchmark(root, name="lab/prepared")

    staging_path = root / ".runtime/benchmark-init.json"
    staging = json.loads(staging_path.read_text())
    first_generation = staging["generation"]
    assert staging["completed"] == [prepared[0]]
    assert len(builds) == 1

    should_fail = False
    state = module.init_benchmark(root)
    assert len(builds) == 1
    assert state["generation"] == first_generation
    assert prepared.count(staging["completed"][0]) == 1
    assert not staging_path.exists()
    assert "completed" not in state


def test_init_rebuilds_missing_box_and_does_not_publish_failed_build(
    monkeypatch, tmp_path
):
    module = builder_module()
    root = init_root(tmp_path)
    monkeypatch.setattr(module, "build_box", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "prepare_scenario_snapshot", lambda *args: None)
    module.init_benchmark(root, name="lab/first")
    monkeypatch.setattr(module, "box_registered", lambda _: False)
    calls = []
    monkeypatch.setattr(module, "build_box", lambda *args, **kwargs: calls.append(args))
    module.init_benchmark(root, name="lab/second")
    assert len(calls) == 1
    before = (root / ".runtime/benchmark.json").read_bytes()

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["vagrant", "up"])

    monkeypatch.setattr(module, "build_box", fail)
    with pytest.raises(subprocess.CalledProcessError):
        module.init_benchmark(root, force=True)
    assert (root / ".runtime/benchmark.json").read_bytes() == before


def test_runner_uses_initialized_box_and_version(monkeypatch, tmp_path):
    from unittest.mock import Mock

    from lab_service.benchmark import init_benchmark

    module = builder_module()
    root = init_root(tmp_path)
    monkeypatch.setattr(module, "build_box", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "prepare_scenario_snapshot", lambda *args: None)
    init_benchmark(root, name="lab/prepared", version="2026.9.7")
    process = Mock(pid=123, returncode=0)
    process.communicate.return_value = (b"ok", b"")
    popen = Mock(return_value=process)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setenv("LAB_VAGRANT_BOX", "")
    monkeypatch.setenv("LAB_OPENCLAW_VERSION", "")
    settings = Settings(root=root, sandbox_token="s" * 32, evaluator_token="e" * 32)
    runner = Runner(settings)
    runner.run(Scenarios(root, "escapes").load("docker-socket"), ["status"], 30)
    env = popen.call_args.kwargs["env"]
    assert env["LAB_VAGRANT_BOX"] == "lab/prepared"
    assert env["LAB_OPENCLAW_VERSION"] == "2026.9.7"


def test_cli_run_requires_init(monkeypatch, tmp_path, capsys):
    module = builder_module()
    monkeypatch.setenv("LAB_ROOT", str(tmp_path))
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/bin/tool")
    with pytest.raises(SystemExit) as exc:
        module.main(["run", "docker-socket"])
    assert exc.value.code == 2
    assert "benchmark init" in capsys.readouterr().err
