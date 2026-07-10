#!/usr/bin/env bash
# Tear down bus-dev. **Preserves** the persistent dev data by default
# (pass --wipe-data to remove it — use when recycling dev from scratch).

set -euo pipefail

DEV_ROOT="${DEV_ROOT:-/mnt/dev/ttoll/dev}"
DATA_DIR="${BUS_DEV_DATA:-${DEV_ROOT}/dev-mirror/state/bus-dev}"
PID_PATH="${DATA_DIR}/bus-dev.pid"
KEEP_DATA=1
[[ "${1:-}" == "--wipe-data" ]] && KEEP_DATA=0
[[ "${1:-}" == "--keep-data" ]] && KEEP_DATA=1  # explicit, backward-compat

if [[ -f "${PID_PATH}" ]]; then
  PID="$(cat "${PID_PATH}")"
  if kill -0 "${PID}" 2>/dev/null; then
    echo "[bus-dev-down] stopping pid ${PID}"
    kill "${PID}" || true
    for _ in 1 2 3 4 5; do
      kill -0 "${PID}" 2>/dev/null || break
      sleep 0.5
    done
    kill -0 "${PID}" 2>/dev/null && { echo "[bus-dev-down] still running; sending SIGKILL"; kill -9 "${PID}" || true; }
  else
    echo "[bus-dev-down] pid ${PID} not alive"
  fi
  rm -f "${PID_PATH}"
else
  echo "[bus-dev-down] no pid file at ${PID_PATH}"
  # Fall back: kill anything matching on the dev db path
  pkill -f "bus.*${DATA_DIR}" 2>/dev/null || true
fi

if [[ "${KEEP_DATA}" -eq 0 ]]; then
  echo "[bus-dev-down] WIPING ${DATA_DIR}"
  rm -rf "${DATA_DIR}"
else
  echo "[bus-dev-down] preserving data at ${DATA_DIR} (pass --wipe-data to remove)"
fi
