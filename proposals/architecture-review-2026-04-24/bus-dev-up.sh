#!/usr/bin/env bash
# Stand up bus-dev on port 8789 with **persistent** data under the
# dev-mirror tree. Isolated from bus-prod (port 8788) in port, data, and
# code (bus-dev runs from the mirror's khonliang-bus via .venv-dev).
#
# Usage:
#   bus-dev-up.sh        # start in foreground (ctrl-c to stop)
#   bus-dev-up.sh -d     # start detached (logs alongside data dir)

set -euo pipefail

DEV_ROOT="${DEV_ROOT:-/mnt/dev/ttoll/dev}"
PORT="${BUS_DEV_PORT:-8789}"
DATA_DIR="${BUS_DEV_DATA:-${DEV_ROOT}/dev-mirror/state/bus-dev}"
BUS_VENV="${BUS_DEV_VENV:-${DEV_ROOT}/.venv-dev}"

DB_PATH="${DATA_DIR}/data/bus-dev.db"
LOG_PATH="${DATA_DIR}/bus-dev.log"
PID_PATH="${DATA_DIR}/bus-dev.pid"

mkdir -p "${DATA_DIR}/data"

if ss -tlnp 2>/dev/null | grep -q ":${PORT}\b"; then
  echo "[bus-dev-up] port ${PORT} already in use; refusing to start"
  exit 1
fi

CMD=("${BUS_VENV}/bin/python" -m bus --port "${PORT}" --db "${DB_PATH}")

if [[ "${1:-}" == "-d" ]]; then
  echo "[bus-dev-up] starting detached on :${PORT}  db=${DB_PATH}  log=${LOG_PATH}"
  nohup "${CMD[@]}" >"${LOG_PATH}" 2>&1 &
  echo $! >"${PID_PATH}"
  # Poll for up to ~5s for the port to bind; uvicorn + schema-init takes a
  # variable fraction of a second on first start with a fresh db.
  bound=0
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if ss -tlnp 2>/dev/null | grep -q ":${PORT}\b"; then bound=1; break; fi
    sleep 0.5
  done
  if [[ "${bound}" -ne 1 ]]; then
    echo "[bus-dev-up] failed to bind port ${PORT} within 5s; last 20 log lines:"
    tail -20 "${LOG_PATH}" >&2
    exit 1
  fi
  echo "[bus-dev-up] running as pid $(cat "${PID_PATH}") on http://localhost:${PORT}/"
else
  echo "[bus-dev-up] starting foreground on :${PORT}  db=${DB_PATH}"
  exec "${CMD[@]}"
fi
