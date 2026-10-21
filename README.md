# Escape Bench

Стенд связывает два проекта:

- `llm_convo_stand` — диалог Agent A/Agent B, история, PostgreSQL и Langfuse;
- `vm_master_stand` — одноразовые VirtualBox/Vagrant VM и sandbox-инструменты Agent B.

## Первый запуск

Нужны Docker Compose, `uv`, Vagrant и VirtualBox. Один раз установите host runner,
подготовьте ускоренный Vagrant box и создайте конфигурацию интеграции:

```bash
cd vm_master_stand
uv run python scripts/install_host_runner.py
uv run benchmark init

cd ../llm_convo_stand
make integration-init
```

`benchmark init` долгий только при первой сборке или изменении входных файлов:
он заранее устанавливает зависимости и сохраняет подготовленный box. Повторный
init использует кеш. `integration-init` создаёт `.env.vm-master` с локальными
токенами и UID/GID пользователя.

## Запуск

```bash
cd llm_convo_stand
make integration-up
```

- интерфейс и API: http://127.0.0.1:8990
- Swagger: http://127.0.0.1:8990/docs
- VM Master: http://127.0.0.1:8991
- Langfuse: http://127.0.0.1:8992

## Проверка

```bash
cd llm_convo_stand
make integration-status
systemctl --user is-active agentic-lab-host-runner.service

curl --noproxy '*' -fsS http://127.0.0.1:8990/health/ready
curl --noproxy '*' -fsS http://127.0.0.1:8990/v1/vm-master/health
curl --noproxy '*' -fsS http://127.0.0.1:8991/healthz
```

## Перезапуск и остановка

После изменений кода пересоберите и обновите контейнеры:

```bash
cd llm_convo_stand
make integration-up
```

Полный перезапуск:

```bash
make integration-down
make integration-up
```

Перезапуск только host runner:

```bash
systemctl --user restart agentic-lab-host-runner.service
```

Полная остановка:

```bash
cd llm_convo_stand
make integration-down
```

Перед `integration-down` завершите активный benchmark через **Finish** или
**Abort**: остановка Docker-контейнеров сама по себе не удаляет запущенную VM.
