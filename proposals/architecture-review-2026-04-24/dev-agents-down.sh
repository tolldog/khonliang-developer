#!/usr/bin/env bash
# Stop the dev fleet of agents. Data dirs are preserved by default.

set -euo pipefail

DEV_ROOT="${DEV_ROOT:-/mnt/dev/ttoll/dev}"
STATE="${STATE:-${DEV_ROOT}/dev-mirror/state}"

AGENTS=(developer-dev researcher-dev librarian-dev reviewer-dev)

for id in "${AGENTS[@]}"; do
  pidfile="${STATE}/${id}/agent.pid"
  if [[ -f "${pidfile}" ]]; then
    pid="$(cat "${pidfile}")"
    if kill -0 "${pid}" 2>/dev/null; then
      echo "[dev-agents-down] stopping ${id} pid=${pid}"
      kill "${pid}" 2>/dev/null || true
      for _ in 1 2 3 4 5; do
        kill -0 "${pid}" 2>/dev/null || break
        sleep 0.5
      done
      kill -0 "${pid}" 2>/dev/null && { echo "  SIGKILL ${id}"; kill -9 "${pid}" 2>/dev/null || true; }
    else
      echo "[dev-agents-down] ${id} not alive (stale pidfile)"
    fi
    rm -f "${pidfile}"
  else
    echo "[dev-agents-down] ${id} no pidfile"
  fi
done
