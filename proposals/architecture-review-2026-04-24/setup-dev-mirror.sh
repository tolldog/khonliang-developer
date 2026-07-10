#!/usr/bin/env bash
# Create the dev-mirror: one git worktree per khonliang-* repo, each on its
# own dev-mirror/<name> branch tracking main. Idempotent — skips worktrees
# that already exist.
#
# The mirror sits at ${DEV_ROOT}/dev-mirror/ alongside the canonical
# checkouts. Prod fleet runs from canonical; dev fleet (bus-dev + dev
# agents) runs from the mirror. Changes in the mirror never reach prod
# unless explicitly merged back upstream.

set -euo pipefail

DEV_ROOT="${DEV_ROOT:-/mnt/dev/ttoll/dev}"
MIRROR="${MIRROR:-${DEV_ROOT}/dev-mirror}"
BASE_BRANCH="${BASE_BRANCH:-main}"

# (name, canonical-checkout-path) pairs
REPOS=(
  "khonliang-bus:${DEV_ROOT}/khonliang-bus"
  "khonliang-bus-lib:${DEV_ROOT}/khonliang-bus-lib"
  "khonliang-developer:${DEV_ROOT}/khonliang-developer"
  "khonliang-researcher:${DEV_ROOT}/khonliang-researcher"
  "khonliang-researcher-lib:${DEV_ROOT}/khonliang-researcher-lib"
  "khonliang-reviewer:${DEV_ROOT}/khonliang-reviewer"
  "khonliang-reviewer-lib:${DEV_ROOT}/khonliang-reviewer-lib"
  "ollama-khonliang:/mnt/dev/ttoll/ollama-khonliang"
)

mkdir -p "${MIRROR}"

for pair in "${REPOS[@]}"; do
  name="${pair%:*}"
  canonical="${pair#*:}"
  target="${MIRROR}/${name}"
  branch="dev-mirror/${name}"

  if [[ -d "${target}" ]]; then
    echo "  (exists) ${name}"
    continue
  fi

  if [[ ! -d "${canonical}/.git" ]]; then
    echo "  (skip)   ${name}: canonical ${canonical} is not a git repo"
    continue
  fi

  if git -C "${canonical}" show-ref --verify --quiet "refs/heads/${branch}"; then
    echo "  (branch exists) ${name} -> checking out ${branch}"
    git -C "${canonical}" worktree add "${target}" "${branch}" 2>&1 | tail -1
  else
    echo "  (new)    ${name} -> new branch ${branch} off ${BASE_BRANCH}"
    git -C "${canonical}" worktree add -b "${branch}" "${target}" "${BASE_BRANCH}" 2>&1 | tail -1
  fi
done

echo "[setup-mirror] mirror ready at ${MIRROR}"
