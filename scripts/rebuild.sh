#!/usr/bin/env bash
# dev 구성 클린 재빌드 + 기동. 사용법:  bash scripts/rebuild.sh
set -eu
cd "$(dirname "$0")/.."

[ -f .env ] || cp .env.example .env

echo "== 사전 확인 =="
echo -n "vendor 휠: "; ls -1 vendor/pip-*.whl 2>/dev/null || echo "(없음!)"
if grep -rq 'python:3.12' docker-compose*.yml ./*/Dockerfile* 2>/dev/null; then
  echo "!!! 아직 python:3.12 참조가 있다. (3.11 이어야 함)"; else echo "베이스 python:3.11 OK"; fi

echo "== 빌드 =="
docker builder prune -af
docker compose -f docker-compose.dev.yml build --no-cache
echo "== 기동 =="
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml ps
echo "== health =="
sleep 3
curl -sS "http://localhost:${AGENT_PORT:-8500}/health" || true
echo
