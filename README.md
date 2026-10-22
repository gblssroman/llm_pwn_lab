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
он сохраняет подготовленный box, создаёт runtime VM и выключенный чистый snapshot.
Повторный init использует box и snapshot из кеша. `integration-init` создаёт
`.env.vm-master` с локальными токенами и UID/GID пользователя.

Каждый benchmark последовательно восстанавливает snapshot, создаёт новый флаг и
запускает выбранный профиль. После прогона snapshot восстанавливается ещё раз,
а VM остаётся выключенной. Host-репозиторий в escape-VM не монтируется.

Сценарий задаёт отдельные `prompt_a` и `prompt_b`. Они становятся system prompts
соответствующих агентов; в интерфейсе можно необязательно дописать инструкции к
каждому из них. Служебные `checks`, `metadata` и конфигурация VM моделям не
передаются.

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
**Abort**: это откатит VM к чистому snapshot и выключит её. Простая остановка
Docker-контейнеров не завершает уже запущенную VM.
