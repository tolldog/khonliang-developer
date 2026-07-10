#!/usr/bin/env bash
# Reset the dev-mirror from prod. For each mirrored repo:
#   1. Stash / abandon any local changes on the dev-mirror/<name> branch.
#   2. Fetch from origin.
#   3. Hard-reset the branch to origin/main (discarding dev's divergence).
# This is how "prod updates dev": canonical gets merged upstream, then this
# script pulls prod state into every mirror in one pass.
#
# SAFE by default: prompts before discarding work. Pass --force to skip
# the prompt (useful for scripted re-creation).

set -euo pipefail

DEV_ROOT="${DEV_ROOT:-/mnt/dev/ttoll/dev}"
MIRROR="${MIRROR:-${DEV_ROOT}/dev-mirror}"
BASE_BRANCH="${BASE_BRANCH:-main}"
FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

if [[ ! -d "${MIRROR}" ]]; then
  echo "[reset-mirror] mirror missing at ${MIRROR}; nothing to reset" >&2
  exit 1
fi

DIRTY=()
for repo_dir in "${MIRROR}"/*/; do
  repo_dir="${repo_dir%/}"
  name="$(basename "${repo_dir}")"
  # Skip non-git dirs (e.g. `state/` which holds bus-dev/agent-dev data).
  [[ -d "${repo_dir}/.git" ]] || continue
  if [[ -n "$(git -C "${repo_dir}" status --porcelain 2>/dev/null)" ]]; then
    DIRTY+=("${name}")
  fi
done

if [[ ${#DIRTY[@]} -gt 0 && ${FORCE} -eq 0 ]]; then
  echo "[reset-mirror] dirty working trees in:"
  for name in "${DIRTY[@]}"; do echo "    ${name}"; done
  echo "  Pass --force to discard all changes, or commit/stash first."
  exit 2
fi

for repo_dir in "${MIRROR}"/*/; do
  repo_dir="${repo_dir%/}"
  name="$(basename "${repo_dir}")"
  # Skip non-git dirs (e.g. `state/`).
  [[ -d "${repo_dir}/.git" ]] || continue
  echo "  - ${name}"
  git -C "${repo_dir}" fetch origin "${BASE_BRANCH}" 2>&1 | tail -1 || true
  git -C "${repo_dir}" reset --hard "origin/${BASE_BRANCH}" 2>&1 | tail -1
done

echo "[reset-mirror] all mirrors reset to origin/${BASE_BRANCH}"
