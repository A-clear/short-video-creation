#!/usr/bin/env bash
# Open WebUI backend の開発起動ラッパー。
#
# v0.11.0 以降 WEBUI_SECRET_KEY が必須で、未設定だと backend が起動しない。
# submodule の backend/dev.sh は upstream のままに保ちたいため、鍵の用意だけを
# ここで済ませてから dev.sh に exec する（start.sh と同じ手順）。
set -euo pipefail

cd "$(dirname "$0")/../full-stack/open-webui/backend"

if [ -f venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
else
  echo "backend/venv がありません。python -m venv venv && pip install -r requirements.txt を先に実行してください。" >&2
  exit 1
fi

KEY_FILE="${WEBUI_SECRET_KEY_FILE:-.webui_secret_key}"

if [ -z "${WEBUI_SECRET_KEY:-}" ] && [ -z "${WEBUI_JWT_SECRET_KEY:-}" ]; then
  if [ ! -f "$KEY_FILE" ]; then
    echo "Generating new WEBUI_SECRET_KEY..."
    head -c "${WEBUI_SECRET_KEY_LENGTH:-24}" /dev/random | base64 > "$KEY_FILE"
  fi

  echo "Loading WEBUI_SECRET_KEY from ${KEY_FILE}"
  WEBUI_SECRET_KEY=$(cat "$KEY_FILE")
  export WEBUI_SECRET_KEY
fi

exec ./dev.sh
