#!/usr/bin/env bash
# 用户态 PostgreSQL 启停（conda 环境内）
set -e
export PATH="$HOME/miniforge3/envs/lighthouse/bin:$PATH"
PGDATA="${PGDATA:-$HOME/pgdata-lighthouse}"
PGPORT="${PGPORT:-5432}"
case "$1" in
  init)
    [ -d "$PGDATA" ] || initdb -D "$PGDATA" -U node --auth=trust >/dev/null
    echo "unix_socket_directories = '/tmp'" >> "$PGDATA/postgresql.conf"
    echo "port = $PGPORT" >> "$PGDATA/postgresql.conf"
    ;;
  start)
    pg_ctl -D "$PGDATA" -l "$PGDATA/server.log" -o "-p $PGPORT" start
    sleep 1
    psql -h /tmp -p $PGPORT -U node -tAc "SELECT 1 FROM pg_database WHERE datname='lighthouse'" | grep -q 1 \
      || createdb -h /tmp -p $PGPORT -U node lighthouse
    ;;
  stop) pg_ctl -D "$PGDATA" stop ;;
  status) pg_ctl -D "$PGDATA" status ;;
  *) echo "usage: $0 init|start|stop|status"; exit 1 ;;
esac
