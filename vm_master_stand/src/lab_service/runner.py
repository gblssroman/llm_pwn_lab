import base64
import hashlib
import ipaddress
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock

from .benchmark import (
    escape_config,
    scenario_snapshot,
    scenario_vagrant_path,
)
from .jobs import log_sink
from .models import ExecResult
from .scenarios import repo_path
from .streaming import collect


def encoded_shell(command: str) -> str:
    """Кодирует пользовательскую команду для передачи через внешний SSH shell.

    Во внешний shell попадает только Base64. Декодирование и выполнение произойдут
    в выбранном контейнере или VM, где команда разрешена. Это сохраняет кавычки,
    переносы строк и Unicode и не даёт содержимому prompt стать командой хоста.
    """
    payload = base64.b64encode(command.encode()).decode("ascii")
    # Only base64 reaches the outer shell. Decoding happens in the shell that
    # owns execution (container for sandbox, VM for evaluator).
    return 'exec sh -c "$(printf %s ' + shlex.quote(payload) + ' | base64 -d)"'


class Runner:
    """Управляет linked clones сценариев и разделяет sandbox/evaluator execution."""

    _guard = threading.Lock()
    _locks = {}

    def __init__(self, settings):
        """Сохраняет проверенные настройки хоста."""
        self.settings = settings
        self._ssh_guard = threading.Lock()
        self._ssh_targets = {}
        self._ssh_generations = {}
        # Never attach to a ControlMaster left by an older host-runner process.
        self._ssh_control_nonce = secrets.token_hex(8)

    def paths(self, attack):
        """Проверяет и возвращает пути к Vagrantfile и provisioning-скрипту сценария."""
        return (
            repo_path(self.settings.root, attack.vagrantfile),
            repo_path(self.settings.root, attack.setup_script),
        )

    def lock_for(self, attack):
        """Возвращает общую блокировку потоков для одного абсолютного Vagrantfile."""
        path, _ = self.paths(attack)
        with self._guard:
            return self._locks.setdefault(str(path), threading.RLock())

    @contextmanager
    def transaction(self, attack):
        """Блокирует общую VM на время составной операции вызывающего кода.

        RLock синхронизирует потоки внутри процесса, FileLock — разные процессы API.
        Имя файла зависит от абсолютного Vagrantfile, поэтому сценарии одной VM
        используют один lock. Повторный захват в том же потоке допустим для lifecycle.
        Сам run не берёт lock: границы операции определяют up/down, verifier и jobs.
        """
        path, _ = self.paths(attack)
        directory = self.settings.root / ".locks"
        directory.mkdir(exist_ok=True)
        name = hashlib.sha256(str(path).encode()).hexdigest()
        with (
            self.lock_for(attack),
            FileLock(
                directory / f"{name}.lock", timeout=self.settings.lifecycle_timeout
            ),
        ):
            yield

    def run(self, attack, args, timeout, environment=None):
        """Запускает Vagrant на хосте и возвращает ExecResult.

        Команда задаётся списком аргументов, без host shell. Environment выбирает
        Vagrantfile, provisioning и подготовленный box; токены control API удаляются.
        При активной фоновой задаче collect передаёт stdout/stderr по мере появления,
        иначе вывод собирается до завершения. Timeout уничтожает локальную группу
        Vagrant/SSH и дочерние процессы, а результат помечается timed_out.
        """
        path, setup = self.paths(attack)
        env = os.environ.copy()
        env.update(environment or {})
        # Every scenario owns a distinct linked clone and clean snapshot.
        env["VAGRANT_DOTFILE_PATH"] = str(
            scenario_vagrant_path(self.settings.root, attack)
        )
        # Vagrant/provisioning must never inherit control API credentials.
        env.pop("LAB_SANDBOX_TOKEN", None)
        env.pop("LAB_EVALUATOR_TOKEN", None)
        box, version = escape_config(
            self.settings.root,
            env.get("LAB_VAGRANT_BOX"),
            env.get("LAB_OPENCLAW_VERSION"),
        )
        env.update(
            LAB_VAGRANT_BOX=box,
            LAB_OPENCLAW_VERSION=version,
            LAB_ISOLATED_ESCAPE="1" if attack.mode == "escape" else "0",
            LAB_LINKED_CLONE="1",
        )
        env.update(
            VAGRANT_CWD=str(path.parent),
            VAGRANT_VAGRANTFILE=path.name,
            LAB_SETUP_SCRIPT=str(setup),
            LAB_REPO_ROOT=str(self.settings.root),
            LAB_ESCAPE_PROFILE=attack.profile if attack.mode == "escape" else "",
        )
        if args and args[0] in {"destroy", "halt", "reload", "snapshot", "up"}:
            self._invalidate_ssh(path, attack.mode, attack.id)
        process = subprocess.Popen(
            ["vagrant", *args],
            cwd=path.parent,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        return self._complete(process, timeout)

    def _complete(self, process, timeout):
        """Собирает локальный процесс с live-логами и жёстким общим timeout."""
        if log_sink.get() is not None:
            return collect(process, timeout, log_sink.get())
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Kill the entire local Vagrant/SSH process group and reap it.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        return ExecResult(
            exit_code=124 if timed_out else process.returncode,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
            timed_out=timed_out,
        )

    def _invalidate_ssh(self, path, mode, attack_id):
        """Забывает endpoint после любого изменения состояния VM."""
        key = (str(path), mode, attack_id)
        with self._ssh_guard:
            self._ssh_targets.pop(key, None)
            # A restored RAM snapshot also rewinds sshd's TCP state. A new
            # ControlPath prevents reuse of a master connection from that state.
            self._ssh_generations[key] = self._ssh_generations.get(key, 0) + 1

    def _ssh_target(self, attack):
        """Один раз читает `vagrant ssh-config` и проверяет локальный endpoint."""
        path, _ = self.paths(attack)
        key = (str(path), attack.mode, attack.id)
        with self._ssh_guard:
            cached = self._ssh_targets.get(key)
        if cached is not None:
            return cached

        # ssh-config содержит локальный путь к ключу; не отправляем его в job logs.
        token = log_sink.set(None)
        try:
            result = self.run(attack, ["ssh-config"], 30)
        finally:
            log_sink.reset(token)
        if result.exit_code or result.timed_out:
            raise RuntimeError(result.stderr or "Unable to read Vagrant SSH config")

        values = {}
        identities = []
        for line in result.stdout.splitlines():
            parts = shlex.split(line.strip())
            if len(parts) < 2:
                continue
            if parts[0] == "IdentityFile":
                identities.append(parts[1])
            elif parts[0] in {"HostName", "User", "Port"}:
                values[parts[0]] = parts[1]
        try:
            hostname = values["HostName"]
            if (
                hostname != "localhost"
                and not ipaddress.ip_address(hostname).is_loopback
            ):
                raise ValueError("SSH endpoint is not loopback")
            user = values["User"]
            if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", user):
                raise ValueError("Invalid SSH user")
            port = int(values["Port"])
            if not 1 <= port <= 65535:
                raise ValueError("Invalid SSH port")
            identity_paths = tuple(str(Path(item).resolve()) for item in identities)
            if not identity_paths or not all(
                Path(item).is_file() for item in identity_paths
            ):
                raise ValueError("Invalid SSH identity")
        except (KeyError, ValueError) as exc:
            raise RuntimeError("Invalid Vagrant SSH config") from exc

        target = (hostname, port, user, identity_paths)
        with self._ssh_guard:
            self._ssh_targets[key] = target
        return target

    def remote(self, attack, command, timeout):
        """Выполняет guest-команду прямым SSH с переиспользуемым ControlMaster.

        Vagrant вызывается только один раз для получения endpoint. Команда передаётся
        отдельным argv без host shell, а SSH доступен лишь через loopback forwarded port.
        """
        if not isinstance(command, str) or not 0 < len(command) <= 200000:
            raise ValueError("Invalid remote command")
        hostname, port, user, identities = self._ssh_target(attack)
        path, _ = self.paths(attack)
        control_key = (str(path), attack.mode, attack.id)
        with self._ssh_guard:
            control_generation = self._ssh_generations.get(control_key, 0)
        control_scope = (
            f"{self.settings.root}\n{attack.mode}\n{attack.id}\n"
            f"{self._ssh_control_nonce}\n{control_generation}"
        )
        root_hash = hashlib.sha256(control_scope.encode()).hexdigest()[:12]
        control_directory = Path(f"/tmp/agentic-lab-{os.getuid()}/{root_hash}")
        control_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        control_directory.chmod(0o700)
        argv = [
            shutil.which("ssh") or "/usr/bin/ssh",
            "-F",
            "/dev/null",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPersist=300",
            "-o",
            f"ControlPath={control_directory}/cm-%C",
        ]
        for identity in identities:
            argv.extend(["-i", identity])
        argv.extend(["-p", str(port), "--", f"{user}@{hostname}", command])
        process = subprocess.Popen(
            argv,
            cwd=self.paths(attack)[0].parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        return self._complete(process, timeout)

    def up(self, attack):
        """Восстанавливает подготовленный clone и активирует готовые контейнеры."""
        with self.transaction(attack):
            started = time.monotonic()
            emit = log_sink.get()
            if emit:
                emit("system", f"[lab] restoring prepared VM for {attack.id}\n")
            snapshot = scenario_snapshot(self.settings.root, attack)
            result = self.run(
                attack,
                ["snapshot", "restore", snapshot],
                self.settings.lifecycle_timeout,
            )
            if result.exit_code or result.timed_out:
                return result
            if attack.mode == "escape":
                # Entropy inside a RAM snapshot is rewound too. Generate the
                # per-run challenge on the host, after the clean restore.
                flag = f"flag{{{secrets.token_hex(20)}}}"
                command = (
                    "sudo env LAB_ESCAPE_PROFILE="
                    + shlex.quote(attack.profile)
                    + " LAB_ESCAPE_FLAG="
                    + shlex.quote(flag)
                    + " LAB_GUEST_WORKSPACE=/opt/agentic-lab "
                    "bash /opt/agentic-lab/vm/escape/activate.sh"
                )
            else:
                command = "sudo bash /workspace/vm/activate.sh"
            result = self.remote(attack, command, self.settings.lifecycle_timeout)
            timing = (
                "[lab] prepared VM restore + activation: "
                f"{time.monotonic() - started:.1f}s\n"
            )
            result.stdout += "\n" + timing
            if emit:
                emit("system", timing)
            return result

    def down(self, attack):
        """Откатывает clone к чистому snapshot и оставляет в Saved state."""
        with self.transaction(attack):
            return self.run(
                attack,
                [
                    "snapshot",
                    "restore",
                    scenario_snapshot(self.settings.root, attack),
                    "--no-start",
                ],
                self.settings.lifecycle_timeout,
            )

    def sandbox(self, attack, request):
        """Выполняет команду внутри sandbox-контейнера выбранного сценария.

        Проверяем фиксированное имя сервиса: YAML не может перенаправить sandbox-токен
        в privileged tool API или БД. Escape выбирает проверенный Compose-профиль.
        Timeout работает внутри контейнера, поэтому обрыв SSH не оставляет обычную
        дочернюю команду без ограничения времени. Внешнему SSH даём ещё 10 секунд.
        """
        service = attack.sandbox_service
        if (
            not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", service)
            or service != "sandbox"
        ):
            raise ValueError("Invalid sandbox service")
        seconds = request.timeout_seconds
        # timeout runs INSIDE the container, so killing SSH cannot leave a
        # normal child command running indefinitely in the remote container.
        compose = "docker compose -f vm/agent-compose.yml"
        if attack.mode == "escape":
            if attack.profile != attack.id:
                raise ValueError("Mismatched escape profile")
            repo_path(self.settings.root, f"vm/escape/profiles/{attack.id}.yml")
            compose = (
                "docker compose -f vm/escape/compose.yml -f vm/escape/profiles/"
                + attack.id
                + ".yml"
            )
        workspace = "/opt/agentic-lab" if attack.mode == "escape" else "/workspace"
        remote = (
            f"cd {workspace} && sudo "
            + compose
            + " exec -T "
            + service
            + f" timeout -k 2s {seconds}s sh -lc "
            + shlex.quote(encoded_shell(request.command))
        )
        result = self.remote(attack, remote, seconds + 15)
        result.timed_out |= result.exit_code in (124, 137)
        return result

    def evaluator(self, attack, request):
        """Выполняет доверенную команду оператора в VM с внутренним timeout и запасом для SSH."""
        seconds = request.timeout_seconds
        remote = f"timeout -k 2s {seconds}s sh -lc " + shlex.quote(
            encoded_shell(request.command)
        )
        result = self.remote(attack, remote, seconds + 15)
        result.timed_out |= result.exit_code in (124, 137)
        return result
