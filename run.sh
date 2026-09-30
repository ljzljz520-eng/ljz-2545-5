#!/usr/bin/env bash
# 一键启动（假设已用 Miniforge 在用户态准备好 conda env 与 PostgreSQL）
set -e
export PATH="$HOME/miniforge3/envs/lighthouse/bin:$PATH"
export LIGHTHOUSE_DSN="${LIGHTHOUSE_DSN:-host=/tmp user=node dbname=lighthouse port=5432}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
./scripts/pg_ctl.sh init
./scripts/pg_ctl.sh start
psql -h /tmp -U node -tAc "SELECT 1 FROM pg_database WHERE datname='lighthouse'" | grep -q 1 \
  || createdb -h /tmp -U node lighthouse
SCENARIO="${1:-default}"
python scripts/init_db.py "$SCENARIO"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
