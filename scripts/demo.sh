#!/usr/bin/env bash
set -e
echo "== 初始化（建表 + 基线示例数据） =="
node scripts/init-db.js --reset
echo
echo "== 一次生成静态行程（日间灯塔游） =="
curl -s -X POST localhost:8080/api/itineraries -H 'content-type: application/json' \
  -d '{"template_key":"lighthouse-day","date":"2026-10-01"}'
echo
echo "启动服务：npm start  然后访问 http://localhost:8080/"
