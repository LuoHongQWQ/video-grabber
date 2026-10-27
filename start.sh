#!/usr/bin/env sh
# Linux / macOS 启动脚本
cd "$(dirname "$0")" || exit 1

PY=python3
if [ -x ".venv/bin/python" ] && .venv/bin/python -c "import fastapi" >/dev/null 2>&1; then
  PY=".venv/bin/python"
fi

exec "$PY" -m uvicorn backend.app:app --host "${VG_HOST:-127.0.0.1}" --port "${VG_PORT:-8000}"
