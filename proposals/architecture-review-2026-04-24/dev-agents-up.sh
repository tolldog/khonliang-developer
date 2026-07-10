#!/usr/bin/env bash
# Stand up the dev fleet of agents (developer-dev, researcher-dev,
# librarian-dev, reviewer-dev) against bus-dev on :8789.
#
# Each agent runs from .venv-dev (editable-installed from dev-mirror),
# uses its per-agent config at dev-mirror/state/<agent>-dev/config.yaml,
# and registers with bus-dev under a "-dev" suffixed agent_type.
#
# Usage:
#   dev-agents-up.sh           # start all in background
#   dev-agents-up.sh status    # report running status
#   dev-agents-up.sh only developer   # start just one (not yet impl — start all)

set -euo pipefail

DEV_ROOT="${DEV_ROOT:-/mnt/dev/ttoll/dev}"
VENV="${VENV:-${DEV_ROOT}/.venv-dev}"
STATE="${STATE:-${DEV_ROOT}/dev-mirror/state}"
BUS_URL="${BUS_URL:-http://localhost:8789}"

# (agent_id, python_entrypoint, config_path)
AGENTS=(
  "developer-dev|developer.agent|${STATE}/developer-dev/config.yaml"
  "researcher-dev|researcher.agent|${STATE}/researcher-dev/config.yaml"
  "librarian-dev|researcher.librarian_agent|${STATE}/researcher-dev/config.yaml"
  "reviewer-dev|reviewer|${STATE}/reviewer-dev/config.yaml"
)

status() {
  echo "dev fleet status:"
  for spec in "${AGENTS[@]}"; do
    local id="${spec%%|*}"
    local pidfile="${STATE}/${id}/agent.pid"
    if [[ -f "${pidfile}" ]] && kill -0 "$(cat "${pidfile}")" 2>/dev/null; then
      echo "  up    ${id} pid=$(cat "${pidfile}")"
    else
      echo "  down  ${id}"
    fi
  done
}

if [[ "${1:-}" == "status" ]]; then
  status
  exit 0
fi

# Precondition: bus-dev must be listening.
if ! ss -tlnp 2>/dev/null | grep -q ":8789\b"; then
  echo "[dev-agents-up] bus-dev is not listening on :8789 — start it first with bus-dev-up.sh -d" >&2
  exit 1
fi

for spec in "${AGENTS[@]}"; do
  IFS='|' read -r id entrypoint config <<<"${spec}"
  state_dir="${STATE}/${id}"
  mkdir -p "${state_dir}"
  pidfile="${state_dir}/agent.pid"
  logfile="${state_dir}/agent.log"

  if [[ -f "${pidfile}" ]] && kill -0 "$(cat "${pidfile}")" 2>/dev/null; then
    echo "[dev-agents-up] ${id} already running pid=$(cat "${pidfile}")"
    continue
  fi

  echo "[dev-agents-up] starting ${id} (entrypoint=${entrypoint})"
  nohup "${VENV}/bin/python" -m "${entrypoint}" \
    --id "${id}" \
    --bus "${BUS_URL}" \
    --config "${config}" \
    >"${logfile}" 2>&1 &
  echo $! >"${pidfile}"
  sleep 0.3
  if ! kill -0 "$(cat "${pidfile}")" 2>/dev/null; then
    echo "[dev-agents-up] ${id} died immediately; log tail:" >&2
    tail -20 "${logfile}" >&2
  fi
done

sleep 1
status

echo
echo "[dev-agents-up] registered agents on bus-dev:"
curl -sS --max-time 3 "${BUS_URL}/v1/services" 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    for agent in d.get('agents', []):
        print(f'  ✓ {agent.get(\"agent_type\")} v{agent.get(\"version\",\"?\")}  skills={len(agent.get(\"skills\",[]))}')
except Exception as e:
    print(f'  (could not parse services: {e})')
"
