#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd "${script_dir}/.." && pwd)"
env_file="${project_dir}/.env.langfuse"

if [[ -f "${env_file}" ]]; then
  echo "${env_file} already exists; leaving credentials unchanged."
  echo "To rotate a fresh, unused installation, move the file away and run this script again."
  exit 0
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required to generate secure local credentials." >&2
  exit 1
fi

admin_email="${LANGFUSE_ADMIN_EMAIL:-admin@localhost.local}"
admin_name="${LANGFUSE_ADMIN_NAME:-Local Admin}"
admin_password="${LANGFUSE_ADMIN_PASSWORD:-$(openssl rand -hex 16)}"
public_key="pk-lf-$(openssl rand -hex 16)"
secret_key="sk-lf-$(openssl rand -hex 24)"

umask 077
cat >"${env_file}" <<EOF
LANGFUSE_IMAGE_TAG=4
LANGFUSE_PORT=8992
LANGFUSE_WORKER_PORT=8993
LANGFUSE_OBJECT_STORAGE_PORT=8994
LANGFUSE_OBJECT_STORAGE_IMAGE=docker.io/chrislusf/seaweedfs:4.48
LANGFUSE_BASE_URL=http://localhost:8992
LANGFUSE_PUBLIC_URL=http://localhost:8992
LANGFUSE_PROJECT_ID=llm-convo-platform
LANGFUSE_TRACING_ENVIRONMENT=development
LANGFUSE_TELEMETRY_ENABLED=false

LANGFUSE_NEXTAUTH_SECRET=$(openssl rand -hex 32)
LANGFUSE_SALT=$(openssl rand -hex 32)
LANGFUSE_ENCRYPTION_KEY=$(openssl rand -hex 32)
LANGFUSE_POSTGRES_USER=postgres
LANGFUSE_POSTGRES_PASSWORD=$(openssl rand -hex 24)
LANGFUSE_POSTGRES_DB=langfuse
LANGFUSE_POSTGRES_VERSION=17
LANGFUSE_CLICKHOUSE_USER=clickhouse
LANGFUSE_CLICKHOUSE_PASSWORD=$(openssl rand -hex 24)
LANGFUSE_REDIS_PASSWORD=$(openssl rand -hex 24)
LANGFUSE_S3_ACCESS_KEY_ID=langfuse
LANGFUSE_S3_SECRET_ACCESS_KEY=$(openssl rand -hex 24)

LANGFUSE_INIT_ORG_ID=llm-convo-org
LANGFUSE_INIT_ORG_NAME=LLM Convo
LANGFUSE_INIT_PROJECT_ID=llm-convo-platform
LANGFUSE_INIT_PROJECT_NAME=LLM Conversation Platform
LANGFUSE_PUBLIC_KEY=${public_key}
LANGFUSE_SECRET_KEY=${secret_key}
LANGFUSE_INIT_USER_EMAIL=${admin_email}
LANGFUSE_INIT_USER_NAME=${admin_name}
LANGFUSE_INIT_USER_PASSWORD=${admin_password}
EOF

chmod 600 "${env_file}"

echo "Created ${env_file} with mode 600."
echo "Langfuse URL: http://localhost:8992"
echo "Admin email: ${admin_email}"
echo "Admin password: ${admin_password}"
echo "The application will load tracing credentials from .env.langfuse automatically."
