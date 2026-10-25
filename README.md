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
попадают в другую. Scenario clones получают стабильные разные loopback SSH-порты
из диапазона `20000–29999`, поэтому несколько прогретых VM не конфликтуют.

Во вкладке **VM Manager** интерфейса Agent Relay можно заранее прогреть отдельные
сценарии или все VM. Тёплый запуск пропускает restore и выполняет только activation. Холодный
первый запуск сам включает lazy warm: после прогона snapshot восстанавливается,
а VM снова прогревается в фоне и остаётся `Ready` до явного `Shutdown`. Выключенная
VM остаётся в Saved state без активной RAM/CPU. Все 12 VM требуют 48 GiB гостевой
RAM плюс overhead VirtualBox. Для escape на хосте создаётся новый флаг;
host-репозиторий в escape-VM не монтируется.

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
**Abort**, затем используйте **VM Manager → Shutdown all**, если нужно освободить RAM.
Простая остановка Docker-контейнеров не завершает уже прогретые VM.
