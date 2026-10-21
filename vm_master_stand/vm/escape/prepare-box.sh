#!/usr/bin/env bash
# Runs only in a separate, newly created builder VM, never an experimental VM.
set -euo pipefail
cd /workspace
source vm/escape/common.sh
lab_stage 'prepare all escape dependencies' lab_dependencies all
lab_stage 'prepare escape image' lab_image
# Runtime escape VMs do not mount the host repository. Copy only the immutable
# files needed for Compose/provisioning into the prepared guest disk.
rm -rf /opt/agentic-lab
install -d -m 755 /opt/agentic-lab/vm
cp -a /workspace/vm/escape /opt/agentic-lab/vm/
cp -a /workspace/vm/openclaw-adapter /opt/agentic-lab/vm/
cp -a /workspace/vm/agent-compose.yml /opt/agentic-lab/vm/
# No challenges, flags, grants, model credentials or state volumes are created.
printf '[lab] prepared box ready: %ss total\n' "$SECONDS"
