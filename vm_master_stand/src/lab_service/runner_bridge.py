"""Unix-socket bridge: Compose UI stays in a container; Vagrant stays on the host."""

import json
import os
import socket
import socketserver
import threading
from pathlib import Path

from .benchmark import escape_config, escape_snapshot, valid_snapshot_name
from .jobs import log_sink
from .models import ExecResult
from .runner import Runner
from .scenarios import Scenarios

_ALLOWED_ENV = ("LAB_VAGRANT_BOX", "LAB_OPENCLAW_VERSION", "LAB_OPENCLAW_COMMAND")


def valid_vagrant_args(args):
    """Разрешает только фиксированные lifecycle/status операции Vagrant."""
    if args in (
        ["up", "--provider=virtualbox", "--provision"],
        ["destroy", "-f"],
        ["status", "--machine-readable"],
        ["provision"],
    ):
        return True
    return (
        isinstance(args, list)
        and len(args) in (3, 4)
        and args[:2] == ["snapshot", "restore"]
        and isinstance(args[2], str)
        and valid_snapshot_name(args[2])
        and (len(args) == 3 or args[3] == "--no-start")
    )


def _validate_request(request):
    """Проверяет форму сообщения, разрешённую команду и ограниченный environment."""
    if not isinstance(request, dict):
        raise ValueError("Invalid bridge request")
    fields = set(request)
    if fields == {"attack_id", "args", "timeout", "environment"}:
        if not valid_vagrant_args(request["args"]):
            raise ValueError("Unsupported Vagrant operation")
        operation = "vagrant"
    elif fields == {"attack_id", "command", "timeout", "environment"}:
        command = request["command"]
        if not isinstance(command, str) or not 0 < len(command) <= 200000:
            raise ValueError("Unsupported remote operation")
        operation = "remote"
    else:
        raise ValueError("Invalid bridge request")

    timeout = request["timeout"]
    # bool — подкласс int в Python, но не допустимое значение timeout.
    if type(timeout) is not int or not 1 <= timeout <= 7300:
        raise ValueError("Unsupported bridge timeout")

    environment = request["environment"]
    if not isinstance(environment, dict):
        raise ValueError("Unsupported Vagrant environment")
    for key, value in environment.items():
        if key not in _ALLOWED_ENV or not isinstance(value, str) or len(value) > 4096:
            raise ValueError("Unsupported Vagrant environment")
    return operation


class BridgeRunner(Runner):
    """Передаёт команды из Docker-панели host runner через защищённый Unix-сокет."""

    def __init__(self, settings, socket_path):
        """Сохраняет настройки и путь к сокету хоста."""
        super().__init__(settings)
        self.socket_path = str(socket_path)

    def _environment(self, attack):
        """Возвращает только разрешённые host-runner overrides."""
        environment = {
            key: os.environ[key] for key in _ALLOWED_ENV if key in os.environ
        }
        if attack.mode == "escape":
            box, version = escape_config(
                self.settings.root,
                environment.get("LAB_VAGRANT_BOX"),
                environment.get("LAB_OPENCLAW_VERSION"),
            )
            environment.update(LAB_VAGRANT_BOX=box, LAB_OPENCLAW_VERSION=version)
        elif not environment.get("LAB_VAGRANT_BOX"):
            environment["LAB_VAGRANT_BOX"] = "bento/ubuntu-24.04"
        return environment

    def _send(self, request, timeout):
        """Отправляет одну Vagrant/SSH операцию и читает поток JSON-сообщений.

        Одно сообщение — одна строка. event направляется в журнал текущей задачи,
        result завершает операцию, error становится RuntimeError для API. Через
        environment передаём только разрешённые настройки box/OpenClaw, без токенов.
        Инициализированный box выбираем и на стороне UI, чтобы старый host runner
        получил уже разрешённые значения вместо пустых overrides.
        """
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout + 30)
                connection.connect(self.socket_path)
                with connection.makefile("rwb") as stream:
                    stream.write((json.dumps(request) + "\n").encode())
                    stream.flush()
                    for line in stream:
                        message = json.loads(line)
                        if message["type"] == "event":
                            emit = log_sink.get()
                            if emit is not None:
                                emit(message["stream"], message["text"])
                        elif message["type"] == "result":
                            return ExecResult.model_validate(message["result"])
                        elif message["type"] == "error":
                            raise RuntimeError(message["error"])
        except (OSError, TimeoutError) as exc:
            raise RuntimeError(
                f"Host runner unavailable at {self.socket_path}: {exc}"
            ) from exc
        raise RuntimeError("Host runner closed the connection without a result")

    def run(self, attack, args, timeout, environment=None):
        """Передаёт разрешённую lifecycle/status операцию Vagrant хосту."""
        request = {
            "attack_id": attack.id,
            "args": args,
            "timeout": timeout,
            "environment": environment or self._environment(attack),
        }
        return self._send(request, timeout)

    def remote(self, attack, command, timeout):
        """Передаёт guest-команду host runner для прямого SSH, минуя Vagrant CLI."""
        request = {
            "attack_id": attack.id,
            "command": command,
            "timeout": timeout,
            "environment": self._environment(attack),
        }
        return self._send(request, timeout)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        """Обрабатывает одну команду клиента на стороне host runner.

        Размер запроса, поля, args, timeout и environment проверяем до запуска Vagrant.
        ID заново загружаем из локального каталога, а не доверяем описанию из контейнера.
        Логи могут приходить из нескольких потоков: отправка каждой JSON-строки
        сериализована отдельным lock. ContextVar восстанавливается даже при ошибке.
        """
        guard = threading.Lock()

        def send(message):
            """Отправляет целую JSON-строку; отключение клиента не обрывает очистку операции."""
            with guard:
                try:
                    self.wfile.write(
                        (json.dumps(message, ensure_ascii=False) + "\n").encode()
                    )
                    self.wfile.flush()
                except OSError:
                    pass

        try:
            line = self.rfile.readline(262145)
            if not line or len(line) > 262144:
                raise ValueError("Invalid bridge request size")
            request = json.loads(line)
            operation = _validate_request(request)
            timeout = request["timeout"]
            extra = request["environment"]
            try:
                attack = self.server.scenarios.load(request["attack_id"])
            except KeyError:
                attack = self.server.escapes.load(request["attack_id"])

            def emit(stream, text):
                """Преобразует блок stdout/stderr в событие протокола."""
                send({"type": "event", "stream": stream, "text": text})

            token = log_sink.set(emit)
            try:
                if operation == "vagrant":
                    args = request["args"]
                    if args[:2] == ["snapshot", "restore"] and (
                        attack.mode != "escape"
                        or args[2] != escape_snapshot(self.server.root)
                    ):
                        raise ValueError("Unsupported VM snapshot")
                    result = self.server.runner.run(
                        attack, args, timeout, environment=extra
                    )
                else:
                    result = self.server.runner.remote(
                        attack, request["command"], timeout
                    )
            finally:
                log_sink.reset(token)
            send({"type": "result", "result": result.model_dump()})
        except Exception as exc:
            send({"type": "error", "error": str(exc)})


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Unix-сервер с отдельным daemon-потоком для каждого подключения."""

    daemon_threads = True


def make_server(root: Path, socket_path: Path, runner):
    """Создаёт host runner socket с доступом только владельцу.

    Каталог имеет режим 0700, сам сокет — 0600. В server сохраняются runner и
    локальные каталоги обеих семей. Удаляется только старый socket path, а не VM
    и не данные бенчмарка; жизненным циклом сервера управляет launcher.
    """
    root = root.resolve()
    directory = socket_path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    if socket_path.exists():
        socket_path.unlink()
    server = _Server(str(socket_path), _Handler)
    socket_path.chmod(0o600)
    server.runner = runner
    server.root = root
    server.scenarios = Scenarios(root)
    server.escapes = Scenarios(root, "escapes")
    return server
