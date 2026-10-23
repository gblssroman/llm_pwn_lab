# Escape Bench

Стенд связывает два проекта:

- `llm_convo_stand` — диалог Agent A/Agent B, история, PostgreSQL и Langfuse;
- `vm_master_stand` — изолированная VirtualBox/Vagrant VM и sandbox-инструменты Agent B.

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
он сохраняет подготовленный box и создаёт для каждого сценария полностью
настроенный linked clone с чистым running-state snapshot. Между прогонами clone
находится в VirtualBox Saved state и не занимает RAM/CPU. Повторный init использует
готовое поколение clones. Если отдельная VM не загрузилась, init до трёх раз
пересоздаёт только её; повторный запуск продолжает незавершённое поколение, не
пересобирая box и уже готовые сценарии. `integration-init` создаёт
`.env.vm-master` с локальными токенами и UID/GID пользователя.

Каждый сценарий использует отдельные Vagrant state, differencing disk и snapshot,
но один общий lock: прогоны остаются последовательными, а изменения одной VM не
попадают в другую.

При запуске backend восстанавливает готовый snapshot, возобновляет сохранённое
состояние VM и проверяет readiness без boot/provisioning/build/pull. Для escape
на хосте создаётся новый флаг. После прогона snapshot восстанавливается ещё раз,
а VM остаётся в Saved state без активной RAM/CPU. Host-репозиторий в escape-VM
не монтируется.

Сценарий задаёт отдельные `prompt_a` и `prompt_b`. Они подставляются в редактируемые
system prompts соответствующих агентов. Служебные `checks`, `metadata` и
конфигурация VM моделям не передаются.

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
**Abort**: это откатит VM к чистому snapshot и переведёт её в Saved state. Простая остановка
Docker-контейнеров не завершает уже запущенную VM.
