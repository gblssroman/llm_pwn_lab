#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd "${script_dir}/.." && pwd)"
env_file="${project_dir}/.env.vm-master"

if [[ -f "${env_file}" ]]; then
  echo "${env_file} already exists; leaving role tokens unchanged."
  echo "Move it away and rerun this script only when you intentionally rotate tokens."
  exit 0
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required to generate secure local role tokens." >&2
  exit 1
fi

evaluator_token="$(openssl rand -hex 32)"
sandbox_token="$(openssl rand -hex 32)"
ui_uid="$(id -u)"
ui_gid="$(id -g)"

umask 077
cat >"${env_file}" <<EOF
LAB_EVALUATOR_TOKEN=${evaluator_token}
LAB_SANDBOX_TOKEN=${sandbox_token}
LAB_UI_UID=${ui_uid}
LAB_UI_GID=${ui_gid}
ESCAPE_BENCH_NETWORK=escape-bench
EOF

chmod 600 "${env_file}"
echo "Created ${env_file} with mode 600."
echo "The same stable role tokens will be supplied to both Compose projects."
