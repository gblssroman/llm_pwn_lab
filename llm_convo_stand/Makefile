LANGFUSE_COMPOSE = docker compose
VM_MASTER_DIR ?= ../vm_master_stand
INTEGRATION_ENV = .env.vm-master
INTEGRATION_NETWORK = escape-bench


.PHONY: up down status logs langfuse-init langfuse-up langfuse-down langfuse-status langfuse-logs langfuse-smoke integration-init integration-network integration-up integration-down integration-status

up: .env.langfuse
	$(LANGFUSE_COMPOSE) up -d --build --wait --wait-timeout 300

down:
	$(LANGFUSE_COMPOSE) down

status:
	$(LANGFUSE_COMPOSE) ps

logs:
	$(LANGFUSE_COMPOSE) logs -f app langfuse-web langfuse-worker

langfuse-init:
	./scripts/init-langfuse.sh

langfuse-up: up

langfuse-down:
	$(MAKE) down

langfuse-status:
	$(MAKE) status

langfuse-logs:
	$(MAKE) logs

langfuse-smoke:
	uv run --extra langfuse python scripts/langfuse-smoke.py

.env.langfuse:
	./scripts/init-langfuse.sh

integration-init:
	./scripts/init-vm-master-env.sh

integration-network:
	docker network inspect $(INTEGRATION_NETWORK) >/dev/null 2>&1 || docker network create $(INTEGRATION_NETWORK)

integration-up: .env.langfuse $(INTEGRATION_ENV) integration-network
	docker compose --env-file $(INTEGRATION_ENV) \
		-p vm-master-stand \
		-f $(VM_MASTER_DIR)/compose.yaml \
		-f docker-compose.vm-master-network.yml \
		up -d --build --wait --wait-timeout 120
	docker compose --env-file $(INTEGRATION_ENV) \
		-p llm-convo-stand \
		-f docker-compose.yml \
		-f docker-compose.vm-master.yml \
		up -d --build --wait --wait-timeout 300

integration-down:
	docker compose --env-file $(INTEGRATION_ENV) -p llm-convo-stand -f docker-compose.yml -f docker-compose.vm-master.yml down
	docker compose --env-file $(INTEGRATION_ENV) -p vm-master-stand -f $(VM_MASTER_DIR)/compose.yaml -f docker-compose.vm-master-network.yml down

integration-status:
	docker compose --env-file $(INTEGRATION_ENV) -p llm-convo-stand -f docker-compose.yml -f docker-compose.vm-master.yml ps
	docker compose --env-file $(INTEGRATION_ENV) -p vm-master-stand -f $(VM_MASTER_DIR)/compose.yaml -f docker-compose.vm-master-network.yml ps

$(INTEGRATION_ENV):
	./scripts/init-vm-master-env.sh
