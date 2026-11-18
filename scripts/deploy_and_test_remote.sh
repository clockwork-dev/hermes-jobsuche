#!/usr/bin/env bash
set -euo pipefail

# Remote deployment and integration test runner.
# Usage:
#   REMOTE_HOST="user@host" [REMOTE_PASS="secret"] ./scripts/deploy_and_test_remote.sh

REMOTE_HOST="${REMOTE_HOST:?Please set REMOTE_HOST (e.g. export REMOTE_HOST=root@example-server)}"
REMOTE_PASS="${REMOTE_PASS:-}"
REMOTE_DIR="${REMOTE_DIR:-/opt/jobsuche-test}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -d "${PROJECT_ROOT}/plugins/hermes/jobsuche" ]]; then
  HERMES_PLUGIN_SRC="${PROJECT_ROOT}/plugins/hermes/jobsuche"
else
  HERMES_PLUGIN_SRC="${HOME}/.hermes/plugins/jobsuche"
fi

SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"

run_ssh() {
  if [[ -n "${REMOTE_PASS}" ]]; then
    sshpass -p "${REMOTE_PASS}" ssh ${SSH_OPTS} "$@"
  else
    ssh ${SSH_OPTS} "$@"
  fi
}

run_rsync() {
  if [[ -n "${REMOTE_PASS}" ]]; then
    sshpass -p "${REMOTE_PASS}" rsync "$@"
  else
    rsync "$@"
  fi
}

echo "[1/4] Preparing remote directory ${REMOTE_HOST}:${REMOTE_DIR} ..."
run_ssh "${REMOTE_HOST}" \
  "mkdir -p '${REMOTE_DIR}' \"\${HOME}/.hermes/plugins/jobsuche\""

echo "[2/4] Syncing project files to ${REMOTE_HOST}:${REMOTE_DIR} ..."
run_rsync -az --delete \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude 'data/' \
  --exclude 'exports/' \
  --exclude '.git/' \
  -e "ssh ${SSH_OPTS}" \
  "${PROJECT_ROOT}/" "${REMOTE_HOST}:${REMOTE_DIR}/"

if [[ -d "${HERMES_PLUGIN_SRC}" ]]; then
  echo "[2b/4] Syncing Hermes plugin to ${REMOTE_HOST}:~/.hermes/plugins/jobsuche ..."
  run_rsync -az --delete \
    --exclude '__pycache__/' \
    -e "ssh ${SSH_OPTS}" \
    "${HERMES_PLUGIN_SRC}/" "${REMOTE_HOST}:~/.hermes/plugins/jobsuche/"
fi

echo "[3/4] Setting up virtualenv and dependencies on ${REMOTE_HOST}:${REMOTE_DIR} ..."
run_ssh "${REMOTE_HOST}" "bash -se" <<EOF
set -euo pipefail
cd "${REMOTE_DIR}"
if [[ ! -x ".venv/bin/python" ]]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e ".[dev]" "mcp>=1.0,<2"
EOF

echo "[4/4] Running pytest and live FISI search trial on ${REMOTE_HOST}:${REMOTE_DIR} ..."
run_ssh "${REMOTE_HOST}" "bash -se" <<EOF
set -euo pipefail
cd "${REMOTE_DIR}"
.venv/bin/pytest tests/test_otp.py tests/test_api_and_db.py tests/test_sorter_exporter.py tests/test_plugins.py
.venv/bin/jobsuche sync --was "Fachinformatiker/in für Systemintegration" --wo "Aachen" --umkreis 25
.venv/bin/jobsuche export
test -s exports/INDEX_FOR_LLM.md
test -s exports/companies_summary.json
EOF

echo "[OK] Remote deployment, test suite, and live FISI export verified on ${REMOTE_HOST}:${REMOTE_DIR}."
