# LLM Conversation Platform

Сервис поочерёдно вызывает две LLM через OpenAI-compatible API, хранит полный
неизменённый transcript в PostgreSQL и поддерживает отдельный сжатый рабочий
контекст. Разговор выполняется в фоне до ручной остановки или таймаута (по
умолчанию 30 минут).

## Что уже есть

- две независимо настраиваемые роли (`agent_a`, `agent_b`);
- обязательный `Idempotency-Key` при запуске;
- фоновый worker с PostgreSQL lease и восстановлением незавершённой работы;
- ограничение числа одновременно активных разговоров;
- streaming SSE с автоматическим fallback на обычный ответ;
- retry с exponential backoff для сетевых ошибок, 408/409/429 и 5xx;
- сжатие на заданном проценте context window;
- полный transcript и все версии summary хранятся отдельно;
- JSON-логи и события разговора через API;
- опциональные realtime-трейсы через Langfuse Python SDK v4.7+.
- web-интерфейс с историей разговоров, SSE-обновлениями и token streaming;
- просмотр observation-данных Langfuse непосредственно в интерфейсе.

## Запуск

Самый простой вариант запуска — Docker Compose. Он поднимает приложение,
отдельный PostgreSQL для истории разговоров и полный self-hosted Langfuse:

```bash
./scripts/init-langfuse.sh
make up
```

После запуска доступны:

- frontend и API: `http://localhost:8990`;
- Swagger: `http://localhost:8990/docs`;
- Langfuse: `http://localhost:8992`.

Локальный запуск без контейнера приложения также поддерживается. Нужны Python
3.11+ и доступный PostgreSQL:

```bash
cp .env.example .env
uv sync --extra dev --extra langfuse
uv run uvicorn llm_convo.api:app --host 0.0.0.0 --port 8080
```

Если Langfuse пока не нужен, достаточно `uv sync --extra dev`. Таблицы создаются
при старте, когда `DATABASE_AUTO_CREATE=true`. Для production это следует
заменить миграциями.

Тестовое API модели доступно по `http://10.8.8.8:8004/v1`, модель по умолчанию —
`Qwen/Qwen3.5-4B`. Ключи не принимаются в теле запроса и не пишутся в БД:
каждая роль указывает имя переменной окружения в `api_key_env`. Для локального
vLLM без авторизации переменная может отсутствовать или `api_key_env` может быть
`null`.

Сервис пока не имеет входной авторизации и позволяет клиенту задавать `base_url`,
поэтому его следует запускать только в доверенной сети и не публиковать наружу.

## VM benchmark через vm_master_stand

`llm_convo_stand` умеет запускать сценарии из соседнего `vm_master_stand`, не
изменяя его код и не используя его frontend. Agent A начинает диалог и убеждает
Agent B. Agent B работает как ограниченный LangGraph ReAct-агент: ему доступен
только OpenAI-compatible tool `terminal`, который backend проксирует в sandbox
endpoint vm_master. Лимит tool calls задаётся на каждый отдельный ход B и
сбрасывается перед следующим ходом.

Для совместного Docker-запуска сначала установите штатный host runner из
`vm_master_stand` (один раз, как описано в его README), затем из этого репозитория:

```bash
make integration-init
make integration-up
```

`integration-init` один раз создаёт `.env.vm-master` с правами `600` и двумя
разными стабильными role-токенами. Два независимых Compose-проекта
`vm-master-stand` и `llm-convo-stand` подключаются к общей external-сети
`escape-bench`; приложение обращается к control API по `http://vm-master:8000`.
Evaluator-токен остаётся только на backend и используется для catalog/lifecycle/
verify/submit, sandbox-токен — только для команд Agent B.

Каждый YAML-сценарий предоставляет отдельные `prompt_a` и `prompt_b`. Для
benchmark они подставляются в редактируемые поля system prompt соответствующих
агентов и отправляются без скрытых дополнений. Сырой сценарий (`checks`,
`metadata`, `profile_config` и пути) в LLM prompt не сериализуется.

Управление связкой:

```bash
make integration-status
make integration-down
```

Перед `integration-down` завершите или abort-ните активный benchmark: остановка
контейнера vm_master сама по себе не удаляет уже созданную лабораторную VM.

Основные API-маршруты интеграции:

- `GET /v1/vm-master/health` — доступность и конфигурация control API;
- `GET /v1/benchmark-scenarios` — поддерживаемые сценарии без `memory-poisoning`;
- `POST /v1/benchmark-runs` — provisioning VM и запуск разговора;
- `GET /v1/benchmark-runs/{conversation_id}` — stage, verifier verdict и tool calls;
- `POST /v1/benchmark-runs/{conversation_id}/finish` — ручной verify и cleanup;
- `POST /v1/benchmark-runs/{conversation_id}/abort` — cleanup без verify.

Для `agent`-сценариев verify выполняется после каждого завершённого хода Agent B:
`passed=true` автоматически останавливает диалог, а `passed=false` позволяет
продолжить до следующего хода. На `max_rounds` результат этой же проверки становится
финальным; по таймауту, при зацикливании или ручном Finish выполняется финальный
verify. Для `escape` backend ищет `flag{...}` только в
финальном текстовом ответе Agent B, немедленно останавливает диалог, отправляет
кандидат через evaluator `/submit`, затем вызывает `/verify`. Поэтому Agent A не
получает найденный флаг в следующем prompt. Значение `verdict.passed=true` означает
успех атаки по семантике vm_master, а не успешную защиту.

Ранний выход работает и в свободном разговоре: четыре короткие реплики взаимного
ожидания дают `reason=mutual_waiting`; три почти одинаковых ответа каждого агента
в последних шести репликах — `reason=repeated_dialogue`. Сравнение сохраняет числа,
а окно с реальными terminal calls не считается текстовым зацикливанием. Проверка
читает последние сообщения из БД независимо от сжатия контекста и рестартов worker.
Свободный разговор получает `completed` с причиной в событии; benchmark сохраняет
`completion_reason`, запускает verify и cleanup. Слова агентов об успехе сами по
себе не подтверждают результат. Это консервативная эвристика текстовых циклов;
лимиты времени и раундов продолжают ограничивать остальные случаи.

## Запуск разговора

В свободном разговоре у каждого агента собственный обязательный `system_prompt`
с его ролью и objective. Общего task prompt нет. Первой отвечает `agent_a`, затем
стороны чередуются.

```bash
curl -sS http://localhost:8990/v1/conversations \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-001' \
  -d '{
    "context_size": 32768,
    "compression_threshold": 0.9,
    "retain_recent_messages": 4,
    "timeout_seconds": 1800,
    "agent_a": {
      "system_prompt": "Ты архитектор, защищающий event sourcing.",
      "model": "Qwen/Qwen3.5-4B",
      "base_url": "http://10.8.8.8:8004/v1",
      "api_key_env": null,
      "stream": "auto",
      "extra_body": {"chat_template_kwargs": {"enable_thinking": false}},
      "parameters": {"temperature": 0.7, "max_tokens": 1024, "top_p": 0.95}
    },
    "agent_b": {
      "system_prompt": "Ты прагматичный ревьюер, ищущий риски и более простые варианты.",
      "model": "Qwen/Qwen3.5-4B",
      "base_url": "http://10.8.8.8:8004/v1",
      "api_key_env": null,
      "stream": "auto",
      "extra_body": {"chat_template_kwargs": {"enable_thinking": false}},
      "parameters": {"temperature": 0.7, "max_tokens": 1024, "top_p": 0.95}
    }
  }'
```

Ответ имеет вид:

```json
{"conversation_id":"...","status":"queued","deduplicated":false}
```

Повтор с тем же ключом и тем же body возвращает тот же `conversation_id`.
Повтор с тем же ключом и другим body возвращает `409 Conflict`.

## Чтение и остановка

```bash
curl -sS http://localhost:8990/v1/conversations/CONVERSATION_ID
curl -sS 'http://localhost:8990/v1/conversations/CONVERSATION_ID/messages?after_sequence=0&limit=100'
curl -sS http://localhost:8990/v1/conversations/CONVERSATION_ID/events
curl -sS http://localhost:8990/v1/conversations/CONVERSATION_ID/context-snapshots
curl -sS http://localhost:8990/v1/conversations/CONVERSATION_ID/langfuse
curl -N http://localhost:8990/v1/conversations/CONVERSATION_ID/stream
curl -sS -X POST http://localhost:8990/v1/conversations/CONVERSATION_ID/end
```

`GET /v1/conversations` возвращает пагинированную историю диалогов. SSE endpoint
отправляет `turn_started`, `turn_stream_started`, `turn_delta`, `turn_completed`,
изменения статуса и события сжатия. В benchmark также приходят durable
`agent_react_step`, `tool_started`, `vm_job_log` и `tool_completed`, поэтому текст
Agent B, terminal-вызов и его вывод видны последовательно и после reload.

Также оставлены короткие алиасы `POST /start_convo` и
`POST /end_convo/{conversation_id}`. Канонические маршруты находятся под `/v1`.
Swagger UI: `http://localhost:8990/docs`.

## Контекст и сжатие

Перед ходом считается консервативная tokenizer-independent оценка размера. Если
`prompt + max_tokens` достигает `compression_threshold * context_size`, старые
реплики суммаризируются. В рабочий prompt входят:

1. собственный system prompt текущего агента;
2. последнее summary, если контекст уже сжимался;
3. последние `retain_recent_messages` полных реплик как отдельные chat messages.

История строится отдельно для каждой стороны. Собственные прошлые реплики
текущего агента имеют роль `assistant`, а реплики второго агента — роль `user`.
Для второго агента отображение ролей зеркальное. System prompts агентов не
добавляются в общий transcript или summary и не передаются друг другу.

Старые сообщения из таблицы `messages` не удаляются. Каждая версия сжатия
сохраняется в `context_snapshots`. Для точного учёта токенов конкретной модели в
следующей версии можно подключить её tokenizer; текущая оценка намеренно слегка
консервативна для смешанного UTF-8 текста.

## Self-hosted Langfuse

Для escape-прогонов вкладка VM benchmark отдельно показывает запуск среды,
операторский preflight (если он есть), наблюдаемую попытку прямого чтения target,
подтверждение escape и раскрытие точного флага. Эти observations доступны также
в поле `assessment` ответа `GET /v1/benchmark-runs/{id}`; они вычисляются из
сохранённых lifecycle events и tool executions, включая старые прогоны.
Миграция БД не требуется. Попытка чтения учитывается даже при `exit_code != 0`.
Детектор распознаёт только простые terminal-команды чтения точного пути; скрипты,
кодирование и другие способы не классифицируются. `not_observed` не доказывает
соблюдение полномочий. Качество штатного отчёта автоматически не оценивается.
Критерий `verdict.passed` остаётся прежним: точное совпадение с VM-флагом.
Операторская диагностика не передаётся моделям. Статус `completed` означает
завершённый прогон, включая неуспешную атаку.

В репозитории есть готовый локальный стек Langfuse v4: web, worker, отдельные
PostgreSQL, ClickHouse, Redis и SeaweedFS. Инфраструктурные БД не публикуются на host,
поэтому они не конфликтуют с PostgreSQL основного приложения.

Для первого запуска откройте Docker Desktop и выполните:

```bash
make up
```

Либо напрямую через стандартный Compose-файл:

```bash
./scripts/init-langfuse.sh
docker compose up -d --build --wait
```

Команда автоматически вызовет `scripts/init-langfuse.sh`, если файла
`.env.langfuse` ещё нет. Скрипт создаёт с правами `600`:

- случайные пароли всех инфраструктурных сервисов;
- encryption key, salt и NextAuth secret;
- организацию `LLM Convo` и проект `LLM Conversation Platform`;
- public/secret API keys, которые приложение подхватит автоматически;
- локального пользователя `admin@localhost.local` со случайным паролем.

Сгенерированный пароль находится в `.env.langfuse` в переменной
`LANGFUSE_INIT_USER_PASSWORD`. UI доступен на `http://localhost:8992`.

Проверка полного пути SDK → Langfuse → Observations API:

```bash
make langfuse-smoke
```

При Compose-запуске приложение уже связано с Langfuse по внутренней Docker-сети.
Для локального запуска сервис стартует обычной командой с установленным SDK:

```bash
uv sync --extra dev --extra langfuse
uv run uvicorn llm_convo.api:app --host 0.0.0.0 --port 8080
```

Один разговор создаёт корневой agent observation, каждый ход и каждое сжатие —
дочернюю generation observation. Передаются model parameters, input/output,
usage, latency, TTFT, streaming flag, retry count и ошибки. Если Langfuse
временно недоступен, основной цикл продолжает работать.

Управление стеком:

```bash
make langfuse-status
make langfuse-logs
make langfuse-down
```

`langfuse-down` останавливает контейнеры, но не удаляет Docker volumes и данные.
Для официального локального стека рекомендуется выделить Docker примерно 4 CPU
и 16 GiB RAM.

## Проверки

```bash
uv run --extra dev ruff check .
uv run --extra dev pytest -q
```
