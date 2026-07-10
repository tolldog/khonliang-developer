#!/usr/bin/env bash
# Bootstrap the shared dev venv with editable installs from the dev-mirror.
# Prereq: dev-mirror must exist (run setup-dev-mirror.sh first).
#
# The venv is pinned to ${DEV_ROOT}/dev-mirror/* paths — NOT the canonical
# checkouts. This gives a complete prod / dev code separation: edits to
# canonical do not affect dev; resetting dev-mirror via git does.
#
# Idempotent: safe to re-run — existing installs are refreshed in place.

set -euo pipefail

DEV_ROOT="${DEV_ROOT:-/mnt/dev/ttoll/dev}"
MIRROR="${MIRROR:-${DEV_ROOT}/dev-mirror}"
VENV="${VENV:-${DEV_ROOT}/.venv-dev}"
PYTHON="${PYTHON:-3.12}"

if [[ ! -d "${MIRROR}" ]]; then
  echo "[bootstrap] mirror missing at ${MIRROR}; run setup-dev-mirror.sh first" >&2
  exit 1
fi

LIBS=(
  "${MIRROR}/ollama-khonliang"
  "${MIRROR}/khonliang-bus-lib"
  "${MIRROR}/khonliang-reviewer-lib"
  "${MIRROR}/khonliang-researcher-lib"
)
AGENTS=(
  "${MIRROR}/khonliang-bus"
  "${MIRROR}/khonliang-reviewer"
  "${MIRROR}/khonliang-researcher"
  "${MIRROR}/khonliang-developer"
)

if [[ ! -d "${VENV}" ]]; then
  echo "[bootstrap] creating venv at ${VENV} (python ${PYTHON})"
  uv venv "${VENV}" --python "${PYTHON}"
else
  echo "[bootstrap] reusing existing venv at ${VENV}"
fi

echo "[bootstrap] installing libs editable from ${MIRROR}"
for repo in "${LIBS[@]}"; do
  [[ -d "${repo}" ]] || { echo "[bootstrap] skip missing: ${repo}"; continue; }
  echo "  - ${repo}"
  uv pip install --python "${VENV}" -e "${repo}" 2>&1 | tail -3
done

echo "[bootstrap] installing agents editable from ${MIRROR}"
for repo in "${AGENTS[@]}"; do
  [[ -d "${repo}" ]] || { echo "[bootstrap] skip missing: ${repo}"; continue; }
  echo "  - ${repo}"
  uv pip install --python "${VENV}" -e "${repo}" 2>&1 | tail -3
done

# Agents' pyproject.toml files git-pin their lib deps, so agent installs
# clobber our editable libs with non-editable git versions. Re-install
# libs now so editable status wins the final arbitration.
echo "[bootstrap] re-installing libs editable (reclaim after agent deps)"
for repo in "${LIBS[@]}"; do
  [[ -d "${repo}" ]] || continue
  echo "  - ${repo}"
  uv pip install --python "${VENV}" -e "${repo}" --force-reinstall --no-deps 2>&1 | tail -2
done

echo "[bootstrap] verifying imports resolve against the mirror"
"${VENV}/bin/python" - <<EOF
import khonliang, khonliang_bus, khonliang_researcher, khonliang_reviewer
import bus, developer, researcher, reviewer
MIRROR = "${MIRROR}"
checks = {
    "khonliang": khonliang.__file__,
    "khonliang_bus": khonliang_bus.__file__,
    "khonliang_researcher": khonliang_researcher.__file__,
    "khonliang_reviewer": khonliang_reviewer.__file__,
    "bus": bus.__file__,
    "developer": developer.__file__,
    "researcher": researcher.__file__,
    "reviewer": reviewer.__file__,
}
bad = []
for name, path in checks.items():
    if MIRROR not in (path or ""):
        bad.append((name, path))
        print(f"  ! {name} -> {path}  (NOT in mirror)")
    else:
        print(f"  ok {name} -> {path}")
if bad:
    raise SystemExit(f"{len(bad)} modules not resolving against mirror")
EOF

echo "[bootstrap] dev venv ready at ${VENV}"
echo "[bootstrap]   all imports resolve against ${MIRROR}"
