#!/usr/bin/env bash
set -euo pipefail

# Installs/links the bundled Antigravity CLI and Hermes Agent plugins into the current user's home directory.

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGY_PLUGIN_SRC="${PROJECT_ROOT}/.agents/plugins/jobsuche"
HERMES_PLUGIN_SRC="${PROJECT_ROOT}/plugins/hermes/jobsuche"

if [[ -d "${AGY_PLUGIN_SRC}" ]]; then
  mkdir -p "${HOME}/.gemini/config/plugins"
  ln -sfn "${AGY_PLUGIN_SRC}" "${HOME}/.gemini/config/plugins/jobsuche"
  echo "[OK] Linked Antigravity CLI plugin -> ${HOME}/.gemini/config/plugins/jobsuche"
fi

if [[ -d "${HERMES_PLUGIN_SRC}" ]]; then
  mkdir -p "${HOME}/.hermes/plugins/jobsuche"
  cp -a "${HERMES_PLUGIN_SRC}/." "${HOME}/.hermes/plugins/jobsuche/"
  echo "[OK] Installed Hermes Agent plugin -> ${HOME}/.hermes/plugins/jobsuche"
  if command -v hermes >/dev/null 2>&1; then
    hermes plugins enable jobsuche || true
  fi
fi
