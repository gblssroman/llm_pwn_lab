"""Prepare a reusable VirtualBox box and isolated per-scenario linked clones."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BOX = "bento/ubuntu-24.04"
DEFAULT_OPENCLAW_VERSION = "2026.9.6"
PREPARATION_TIMEOUT = 7200
SNAPSHOT_NAME = "agentic-lab-clean"
SCENARIO_PREPARATION_ATTEMPTS = 3
STAGING_STATE_NAME = "benchmark-init.json"

# Изменение любого из этих файлов требует новой подготовки VM-образа.
BOX_INPUTS = (
    "vagrant/Vagrantfile",
    "vm/activate.sh",
    "vm/setup.sh",
    "vm/start-api-proxy.sh",
    "vm/escape/common.sh",
    "vm/escape/activate.sh",
    "vm/escape/prepare-box.sh",
    "vm/escape/setup.sh",
    "vm/escape/Dockerfile",
    "vm/escape/compose.yml",
    "vm/escape/profiles/dac-read-search.yml",
    "vm/escape/profiles/docker-socket.yml",
    "vm/escape/profiles/hostpath-etc.yml",
    "vm/escape/profiles/pid-namespace.yml",
    "vm/escape/profiles/privileged.yml",
    "vm/escape/profiles/sys-admin.yml",
    "vm/escape/profiles/sys-module.yml",
    "vm/agent-compose.yml",
    "vm/openclaw-adapter/Dockerfile",
    "vm/openclaw-adapter/adapter.py",
    "vm/openclaw-adapter/lab_tool.py",
    "vm/postgres-init.sql",
    "vm/target-api/Dockerfile",
    "vm/target-api/app.py",
    "vm/target-api/requirements.txt",
)


def vagrant_dotfile_path(root, mode, scenario_id=None, generation=None):
    """Возвращает изолированный Vagrant state семьи или конкретного сценария."""
    if mode not in {"agent", "escape"}:
        raise ValueError("Unknown benchmark mode")
    if scenario_id is None:
        return root / ".runtime" / "vagrant" / mode
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", scenario_id):
        raise ValueError("Invalid scenario ID")
    if generation is None:
        generation = _benchmark_generation(root)
    if not isinstance(generation, str) or not re.fullmatch(
        r"[0-9]{8}-[0-9]{6}-[0-9]{6}", generation
    ):
        raise RuntimeError("Invalid benchmark generation; run benchmark init again")
    return root / ".runtime" / "vagrant" / "scenarios" / generation / mode / scenario_id


def scenario_key(attack):
    """Даёт стабильный ключ scenario snapshot в benchmark state."""
    return f"{attack.mode}:{attack.id}"


def _benchmark_generation(root):
    """Читает опубликованное поколение подготовленных linked clones."""
    try:
        data = json.loads((root / ".runtime/benchmark.json").read_text())
        generation = data["generation"]
        scenarios = data["scenarios"]
        if not isinstance(scenarios, dict):
            raise TypeError("Invalid scenarios")
        return generation
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(
            "Prepared scenario VMs are missing; run benchmark init again"
        ) from exc


def scenario_vagrant_path(root, attack, generation=None):
    """Возвращает metadata path linked clone выбранного сценария."""
    return vagrant_dotfile_path(root, attack.mode, attack.id, generation)


def scenario_snapshot(root, attack):
    """Возвращает snapshot только для опубликованного подготовленного сценария."""
    state = root / ".runtime/benchmark.json"
    try:
        data = json.loads(state.read_text())
        _ = vagrant_dotfile_path(
            root, attack.mode, attack.id, data["generation"]
        )
        snapshot = data["scenarios"][scenario_key(attack)]
        if not isinstance(snapshot, str) or not valid_snapshot_name(snapshot):
            raise ValueError("Invalid snapshot name")
        return snapshot
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(
            f"Prepared VM for {attack.id} is missing; run benchmark init again"
        ) from exc


def build_box(base_box, name, output, version, root=None):
    """Создаёт и регистрирует подготовленный VirtualBox box в отдельной VM.

    base_box — исходная Ubuntu; name — имя новой регистрации; output — файл .box;
    version — версия OpenClaw для Docker-образа. Рабочая VM эксперимента не участвует:
    у builder свой VAGRANT_CWD и свои metadata в .cache/escape-boxes.

    Последовательность: provisioning, перезагрузка для новых kernel/headers,
    проверка готовности, упаковка, регистрация. Environment очищен от LAB-переменных
    оператора, чтобы токены и команда модели не попали в подготовленное окружение.
    В finally удаляется только builder VM. Готовый box и файл сохраняются;
    сбой cleanup печатает путь metadata для ручной диагностики.
    """
    root = (root or ROOT).resolve()
    if output.exists():
        raise ValueError(f"Refusing to overwrite {output}")
    cache = root / ".cache" / "escape-boxes"
    cache.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="builder-", dir=cache))
    output.parent.mkdir(parents=True, exist_ok=True)
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("LAB_")
    }
    env.update(
        VAGRANT_CWD=str(work),
        VAGRANT_VAGRANTFILE=str(root / "vagrant/Vagrantfile"),
        LAB_REPO_ROOT=str(root),
        LAB_SETUP_SCRIPT=str(root / "vm/escape/prepare-box.sh"),
        LAB_VAGRANT_BOX=base_box,
        LAB_BUILD_ESCAPE_BOX="1",
        LAB_ESCAPE_PROFILE="",
        LAB_OPENCLAW_VERSION=version,
        LAB_OPENCLAW_COMMAND="",
    )

    def run(*args):
        """Запускает один шаг Vagrant в builder и печатает длительность даже при ошибке."""
        started = time.monotonic()
        print(f"[box] starting: {' '.join(args)}", flush=True)
        try:
            subprocess.run(
                ["vagrant", *args],
                cwd=work,
                env=env,
                check=True,
                timeout=PREPARATION_TIMEOUT,
            )
        finally:
            print(f"[box] {args[0]}: {time.monotonic() - started:.1f}s", flush=True)

    try:
        run("up", "--provider=virtualbox", "--provision")
        run("reload", "--no-provision")
        run(
            "ssh",
            "-c",
            "test -d /lib/modules/$(uname -r)/build && sudo docker info >/dev/null",
        )
        run("package", "--output", str(output))
        run("box", "add", "--name", name, "--provider", "virtualbox", str(output))
    finally:
        # This CWD belongs only to the builder, not vagrant/'s benchmark VM.
        try:
            run("destroy", "-f")
        except (subprocess.SubprocessError, OSError) as exc:
            print(
                f"Builder cleanup failed: {exc}. Builder metadata: {work}", flush=True
            )
    print(
        f"\nBox registered: {name}\nBox file for your colleague: {output}", flush=True
    )


def escape_config(root, box_override=None, version_override=None):
    """Возвращает пару (box, версия OpenClaw) для benchmark-прогона.

    Читаем один JSON-снимок, чтобы не смешать имя от старой настройки с версией
    от новой. Явные environment overrides имеют приоритет; до init доступны
    базовый Ubuntu box и версия CLI по умолчанию. Повреждённую настройку считаем
    ошибкой: молча запускать другое окружение для бенчмарка нельзя.
    """
    state = root / ".runtime/benchmark.json"
    data = {}
    try:
        if state.exists():
            data = json.loads(state.read_text())
        box = box_override or data.get("box", "bento/ubuntu-24.04")
        version = version_override or data.get("openclaw_version", "2026.9.6")
        if not isinstance(box, str) or not box.strip() or len(box) > 256:
            raise ValueError("Invalid box name")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("Invalid OpenClaw version")
        return box, version
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RuntimeError("Invalid benchmark state; run benchmark init again") from exc


def valid_snapshot_name(value):
    """Ограничивает snapshot безопасным именем, передаваемым Vagrant как argv."""
    return 0 < len(value) <= 64 and all(
        character.isalnum() or character in "._-" for character in value
    )


def selected_box(root, override=None):
    """Возвращает только имя box; используется при проверке CLI перед запуском."""
    return escape_config(root, override)[0]


def box_registered(name):
    """Проверяет наличие имени среди локальных VirtualBox boxes без запуска VM."""
    result = subprocess.run(
        ["vagrant", "box", "list"], check=True, capture_output=True, text=True
    )
    return any(
        line.startswith(name + " (") and "(virtualbox," in line
        for line in result.stdout.splitlines()
    )


def _box_fingerprint(root, base_box, version):
    """Вычисляет хеш рецепта подготовки: файлов, базового box и версии CLI."""
    fingerprint = hashlib.sha256((base_box + "\n" + version + "\n").encode())
    for relative_path in BOX_INPUTS:
        contents = (root / relative_path).read_bytes()
        fingerprint.update(relative_path.encode() + b"\0" + contents)
    return fingerprint.hexdigest()


def _read_previous_state(state_path):
    """Читает прошлую настройку; отсутствующий или повреждённый JSON даёт пустой кеш."""
    try:
        return json.loads(state_path.read_text())
    except (OSError, ValueError):
        return {}


def _can_reuse_box(previous, fingerprint, name, output):
    """Проверяет рецепт, явные параметры пользователя и регистрацию box в Vagrant."""
    if not isinstance(previous, dict):
        return False
    if previous.get("fingerprint") != fingerprint:
        return False
    if name is not None and previous.get("box") != name:
        return False
    if output is not None and previous.get("box_file") != str(output.resolve()):
        return False

    box_name = previous.get("box")
    return isinstance(box_name, str) and box_registered(box_name)


def _save_state(state_path, data):
    """Атомарно заменяет настройки, чтобы runner не прочитал половину JSON."""
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=state_path.parent,
        prefix="benchmark-",
        suffix=".json",
        delete=False,
    ) as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")
        temporary_path = Path(stream.name)
    os.replace(temporary_path, state_path)


def _can_resume_initialization(
    candidate, fingerprint, expected_scenarios, name, output
):
    """Проверяет незавершённое поколение перед продолжением init."""
    if not isinstance(candidate, dict):
        return False
    if candidate.get("fingerprint") != fingerprint:
        return False
    if candidate.get("scenarios") != expected_scenarios:
        return False
    if candidate.get("snapshot") != SNAPSHOT_NAME:
        return False
    generation = candidate.get("generation")
    if not isinstance(generation, str) or not re.fullmatch(
        r"[0-9]{8}-[0-9]{6}-[0-9]{6}", generation
    ):
        return False
    completed = candidate.get("completed")
    if not isinstance(completed, list) or not set(completed) <= set(
        expected_scenarios
    ):
        return False
    box_name = candidate.get("box")
    box_file = candidate.get("box_file")
    if not isinstance(box_name, str) or not box_name:
        return False
    if not isinstance(box_file, str) or not Path(box_file).is_absolute():
        return False
    if name is not None and box_name != name:
        return False
    if output is not None and box_file != str(output.resolve()):
        return False
    return box_registered(box_name)


def _scenario_environment(root, attack, box_name, version, generation):
    """Строит окружение отдельного linked clone конкретного сценария."""
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("LAB_")
    }
    environment.update(
        VAGRANT_CWD=str(root / "vagrant"),
        VAGRANT_VAGRANTFILE="Vagrantfile",
        VAGRANT_DOTFILE_PATH=str(
            scenario_vagrant_path(root, attack, generation)
        ),
        LAB_REPO_ROOT=str(root),
        LAB_SETUP_SCRIPT=str(root / attack.setup_script),
        LAB_VAGRANT_BOX=box_name,
        LAB_OPENCLAW_VERSION=version,
        LAB_OPENCLAW_COMMAND="",
        LAB_ESCAPE_PROFILE=attack.profile if attack.mode == "escape" else "",
        LAB_ISOLATED_ESCAPE="1" if attack.mode == "escape" else "0",
        LAB_LINKED_CLONE="1",
    )
    return environment


def _runtime_vagrant(root, environment, *args, capture=False):
    """Запускает один ограниченный шаг подготовки runtime VM."""
    started = time.monotonic()
    print(f"[vm] starting: {' '.join(args)}", flush=True)
    try:
        return subprocess.run(
            ["vagrant", *args],
            cwd=root / "vagrant",
            env=environment,
            check=True,
            timeout=PREPARATION_TIMEOUT,
            capture_output=capture,
            text=capture,
        )
    finally:
        print(f"[vm] {args[0]}: {time.monotonic() - started:.1f}s", flush=True)


def _power_off_failed_clone(dotfile, attack):
    """Адресно останавливает зависший VBoxHeadless до удаления failed clone."""
    try:
        vm_id = (
            dotfile / "machines" / "default" / "virtualbox" / "id"
        ).read_text().strip()
        if not re.fullmatch(
            r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", vm_id
        ):
            return
        executable = shutil.which("VBoxManage") or "VBoxManage"
        info = subprocess.run(
            [executable, "showvminfo", vm_id, "--machinereadable"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        state = re.search(r'^VMState="([^"]+)"$', info.stdout, re.MULTILINE)
        if info.returncode or state is None or state.group(1) not in {
            "running",
            "paused",
            "stuck",
            "guru_meditation",
            "live_snapshotting",
        }:
            return
        stopped = subprocess.run(
            [executable, "controlvm", vm_id, "poweroff"],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if stopped.returncode:
            print(
                f"[scenario] unable to power off failed "
                f"{attack.mode}:{attack.id} clone",
                flush=True,
            )
        else:
            print(
                f"[scenario] powered off failed {attack.mode}:{attack.id} clone",
                flush=True,
            )
    except (OSError, subprocess.SubprocessError):
        # The following Vagrant destroy remains the authoritative cleanup path.
        return


def _scenario_snapshot_available(
    root, attack, box_name, version, generation, snapshot
):
    """Проверяет linked clone сценария и наличие его чистого snapshot."""
    if not isinstance(snapshot, str) or not valid_snapshot_name(snapshot):
        return False
    try:
        environment = _scenario_environment(
            root, attack, box_name, version, generation
        )
        status = _runtime_vagrant(
            root, environment, "status", "--machine-readable", capture=True
        )
        if ",state,not_created" in status.stdout:
            return False
        snapshots = _runtime_vagrant(
            root, environment, "snapshot", "list", capture=True
        )
        return snapshot in {line.strip() for line in snapshots.stdout.splitlines()}
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return False


def prepare_scenario_snapshot(
    root, attack, box_name, version, generation, snapshot=SNAPSHOT_NAME
):
    """Готовит clone и сохраняет чистое состояние работающей VM на диске."""
    environment = _scenario_environment(
        root, attack, box_name, version, generation
    )
    dotfile = Path(environment["VAGRANT_DOTFILE_PATH"])
    dotfile.mkdir(mode=0o700, parents=True, exist_ok=True)
    dotfile.chmod(0o700)
    print(f"[scenario] preparing {attack.mode}:{attack.id}", flush=True)
    workspace = "/opt/agentic-lab" if attack.mode == "escape" else "/workspace"
    compose = (
        "vm/escape/compose.yml"
        if attack.mode == "escape"
        else "vm/agent-compose.yml"
    )
    for attempt in range(1, SCENARIO_PREPARATION_ATTEMPTS + 1):
        _runtime_vagrant(root, environment, "destroy", "-f")
        try:
            _runtime_vagrant(
                root,
                environment,
                "up",
                "--provider=virtualbox",
                "--provision",
            )
            _runtime_vagrant(
                root,
                environment,
                "ssh",
                "-c",
                f"cd {workspace} && sudo docker info >/dev/null && "
                f"sudo docker compose -f {compose} ps --status running --services "
                "| grep -qx sandbox",
            )
            # A running VirtualBox snapshot includes RAM/CPU/device state. Restore
            # without start releases host RAM while preserving the resume point.
            _runtime_vagrant(root, environment, "snapshot", "save", "-f", snapshot)
            _runtime_vagrant(
                root, environment, "snapshot", "restore", snapshot, "--no-start"
            )
            return
        except subprocess.SubprocessError as exc:
            _power_off_failed_clone(dotfile, attack)
            if attempt < SCENARIO_PREPARATION_ATTEMPTS:
                print(
                    f"[scenario] {attack.mode}:{attack.id} attempt "
                    f"{attempt}/{SCENARIO_PREPARATION_ATTEMPTS} failed: {exc}; "
                    "recreating only this clone",
                    flush=True,
                )
                continue
            try:
                _runtime_vagrant(root, environment, "destroy", "-f")
            except (OSError, subprocess.SubprocessError) as cleanup_error:
                print(
                    f"[scenario] cleanup failed for {attack.mode}:{attack.id}: "
                    f"{cleanup_error}",
                    flush=True,
                )
            raise


def init_benchmark(
    root,
    base_box=DEFAULT_BOX,
    version=DEFAULT_OPENCLAW_VERSION,
    name=None,
    output=None,
    force=False,
):
    """Готовит box и linked clone в Saved state для каждого сценария.

    Сначала сравниваем рецепт с предыдущей успешной сборкой. Если совпадают
    файлы и параметры, а box всё ещё зарегистрирован, дорогая сборка не нужна.
    force явно отключает это повторное использование.

    Файловые блокировки исключают параллельный init и запуск сценария. Новое
    поколение clones публикуется только после успешной подготовки всех VM: при
    ошибке runner продолжит видеть прошлое поколение, а следующий init продолжит
    staging-поколение. Связанные диски разделяют только неизменяемый base;
    состояние каждого сценария и snapshot независимы.
    """
    from .scenarios import Scenarios

    root = root.resolve()
    fingerprint = _box_fingerprint(root, base_box, version)
    scenarios = [*Scenarios(root).all(), *Scenarios(root, "escapes").all()]
    expected_scenarios = {scenario_key(item): SNAPSHOT_NAME for item in scenarios}
    locks_directory = root / ".locks"
    locks_directory.mkdir(exist_ok=True)
    state_path = root / ".runtime/benchmark.json"
    staging_path = root / ".runtime" / STAGING_STATE_NAME
    vagrantfile = (root / "vagrant/Vagrantfile").resolve()
    lifecycle_lock = hashlib.sha256(str(vagrantfile).encode()).hexdigest()

    with (
        FileLock(locks_directory / "benchmark-init.lock", timeout=PREPARATION_TIMEOUT),
        FileLock(
            locks_directory / f"{lifecycle_lock}.lock", timeout=PREPARATION_TIMEOUT
        ),
    ):
        previous = _read_previous_state(state_path)
        reuse_box = not force and _can_reuse_box(previous, fingerprint, name, output)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        snapshot = SNAPSHOT_NAME
        previous_generation = previous.get("generation")
        previous_box_name = previous.get("box")
        reuse_scenarios = (
            reuse_box
            and previous.get("scenarios") == expected_scenarios
            and isinstance(previous_generation, str)
            and all(
                _scenario_snapshot_available(
                    root,
                    scenario,
                    previous_box_name,
                    version,
                    previous_generation,
                    snapshot,
                )
                for scenario in scenarios
            )
        )
        if reuse_scenarios:
            staging_path.unlink(missing_ok=True)
            print(f"Benchmark already initialized: {previous_box_name}", flush=True)
            return previous

        staging = _read_previous_state(staging_path)
        resume = not force and _can_resume_initialization(
            staging, fingerprint, expected_scenarios, name, output
        )
        if resume:
            box_name = staging["box"]
            box_file = Path(staging["box_file"])
            generation = staging["generation"]
            completed = set(staging["completed"])
            print(
                f"[init] resuming generation {generation}: "
                f"{len(completed)}/{len(scenarios)} scenarios recorded",
                flush=True,
            )
        else:
            if reuse_box:
                box_name = previous["box"]
                box_file = Path(previous["box_file"])
            else:
                # Уникальное имя сохраняет прошлую регистрацию, пока новая не готова.
                box_name = name or f"agentic-lab/escape-{stamp}"
                box_file = (
                    output
                    or root / ".cache/escape-boxes" / f"escape-{stamp}.box"
                )
                box_file = box_file.resolve()
                build_box(base_box, box_name, box_file, version, root=root)
            generation = stamp
            completed = set()
            staging = {
                "box": box_name,
                "snapshot": snapshot,
                "generation": generation,
                "scenarios": expected_scenarios,
                "fingerprint": fingerprint,
                "openclaw_version": version,
                "base_box": base_box,
                "box_file": str(box_file),
                "completed": [],
            }
            _save_state(staging_path, staging)

        for scenario in scenarios:
            key = scenario_key(scenario)
            dotfile = scenario_vagrant_path(root, scenario, generation)
            if (key in completed or dotfile.exists()) and _scenario_snapshot_available(
                root, scenario, box_name, version, generation, snapshot
            ):
                if key not in completed:
                    completed.add(key)
                    staging["completed"] = [
                        item for item in expected_scenarios if item in completed
                    ]
                    _save_state(staging_path, staging)
                print(f"[scenario] reusing {key}", flush=True)
                continue
            prepare_scenario_snapshot(
                root, scenario, box_name, version, generation, snapshot
            )
            completed.add(key)
            staging["completed"] = [
                item for item in expected_scenarios if item in completed
            ]
            _save_state(staging_path, staging)
        state = {key: value for key, value in staging.items() if key != "completed"}
        _save_state(state_path, state)
        staging_path.unlink(missing_ok=True)

    print(
        "Benchmark initialized with per-scenario linked clones. "
        "Run: uv run benchmark run docker-socket",
        flush=True,
    )
    return state


def _make_parser():
    """Описывает три команды CLI и их параметры."""
    parser = argparse.ArgumentParser(
        description="Initialize and run local escape benchmarks."
    )
    commands = parser.add_subparsers(dest="action", required=True)

    initialize = commands.add_parser(
        "init", help="Prepare the box and clean linked clone for every scenario"
    )
    initialize.add_argument("--base-box", default=DEFAULT_BOX)
    initialize.add_argument("--name")
    initialize.add_argument("--output", type=Path)
    initialize.add_argument(
        "--openclaw-version",
        default=os.environ.get("LAB_OPENCLAW_VERSION") or DEFAULT_OPENCLAW_VERSION,
    )
    initialize.add_argument(
        "--force",
        action="store_true",
        help="Build a new box even when current inputs match",
    )

    run = commands.add_parser(
        "run", help="Run OpenClaw on an escape scenario using the initialized box"
    )
    run.add_argument("scenario")
    run.add_argument("--keep-vm", action="store_true")
    commands.add_parser("list", help="List the seven local escape scenarios")
    return parser


def _run_benchmark(root, args, parser):
    """Проверяет init и передаёт запуск существующему OpenClaw runner.

    CLI отвечает за выбор подготовленного окружения. Сам цикл up → запуск
    агента → verifier → down остаётся в run_openclaw_escape.py. Передаём настройки
    аргументами и environment; shell не используется. Код завершения runner
    становится кодом завершения команды benchmark run.
    """
    state_path = root / ".runtime/benchmark.json"
    if not state_path.is_file():
        parser.error("Run benchmark init before running a benchmark")

    box_name = selected_box(root, os.environ.get("LAB_VAGRANT_BOX"))
    if not box_registered(box_name):
        parser.error("Prepared box is missing; run benchmark init again")

    state = json.loads(state_path.read_text())
    environment = os.environ.copy()
    environment["LAB_VAGRANT_BOX"] = box_name
    environment["LAB_OPENCLAW_VERSION"] = (
        os.environ.get("LAB_OPENCLAW_VERSION") or state["openclaw_version"]
    )
    command = [
        sys.executable,
        str(root / "scripts/run_openclaw_escape.py"),
        args.scenario,
    ]
    if args.keep_vm:
        command.append("--keep-vm")

    result = subprocess.run(command, cwd=root, env=environment)
    raise SystemExit(result.returncode)


def main(argv=None):
    """Разбирает CLI, проверяет инструменты хоста и выбирает init/run/list."""
    root = Path(os.environ.get("LAB_ROOT") or ROOT).resolve()
    parser = _make_parser()
    args = parser.parse_args(argv)

    try:
        # Каталог можно посмотреть до установки Vagrant/VirtualBox.
        if args.action == "list":
            from .scenarios import Scenarios

            scenarios = Scenarios(root, "escapes").all()
            print("\n".join(scenario.id for scenario in scenarios))
            return

        missing = [tool for tool in ("vagrant", "VBoxManage") if not shutil.which(tool)]
        if missing:
            parser.error("Install host tools first: " + ", ".join(missing))

        if args.action == "init":
            init_benchmark(
                root,
                base_box=args.base_box,
                version=args.openclaw_version,
                name=args.name,
                output=args.output,
                force=args.force,
            )
        else:
            _run_benchmark(root, args, parser)
    except (
        ValueError,
        KeyError,
        RuntimeError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        parser.exit(1, f"Benchmark {args.action} failed: {exc}\n")


if __name__ == "__main__":
    main()
