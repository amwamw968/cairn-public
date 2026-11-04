#!/usr/bin/env bash
# One command to start Cairn 0.1 locally.
#
#   ./scripts/start.sh              # http://127.0.0.1:8000
#   PORT=8080 ./scripts/start.sh    # another port
#
# Uses ./.venv when present; extra args are passed to uvicorn (e.g. --reload).
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -x ".venv/bin/uvicorn" ]; then
  echo "未找到 .venv，请先安装依赖：" >&2
  echo "  python3.12 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

PORT="${PORT:-8000}"
echo "Cairn 启动中：http://127.0.0.1:${PORT}"
exec .venv/bin/uvicorn cairn.web:app --host 127.0.0.1 --port "${PORT}" "$@"
