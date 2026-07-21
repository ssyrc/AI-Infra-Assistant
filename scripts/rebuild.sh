#!/usr/bin/env bash
# dev 구성 빌드 + 기동.
# 폐쇄망 미러/프록시가 '동시 요청'에 간헐적으로 빈 응답(-> pip "from versions: none")을 주므로,
# 8개 이미지를 병렬로 몰지 않고 '한 서비스씩 순차' 빌드하고, 실패하면 자동 재시도한다.
#
# 사용법:
#   bash scripts/rebuild.sh            # 순차+재시도 빌드 후 기동
#   NOCACHE=1 bash scripts/rebuild.sh  # 캐시 없이 처음부터
set -u
cd "$(dirname "$0")/.."
COMPOSE="docker compose -f docker-compose.dev.yml"
MAX_TRY=5

[ -f .env ] || cp .env.example .env

echo "== 사전 확인 =="
echo -n "vendor 휠: "; ls -1 vendor/pip-*.whl 2>/dev/null || echo "(없음!)"
if grep -rq 'python:3.12' docker-compose*.yml ./*/Dockerfile* 2>/dev/null; then
  echo "!!! 아직 python:3.12 참조가 있다(3.11 이어야 함)"; fi

# 빌드 대상(= build: 있는 서비스). 의존 순서상 db-init/mock 먼저.
SERVICES="db-init mock-vllm manual-mcp command-mcp voc-mcp system-mcp agent-server admin-console"

if [ "${NOCACHE:-0}" = "1" ]; then
  echo "== 캐시 정리 =="; docker builder prune -af >/dev/null 2>&1 || true
fi

FAILED=""
for svc in $SERVICES; do
  echo
  echo "======== 빌드: $svc ========"
  n=1
  # 병렬 억제: 한 번에 한 서비스만. 실패 시(대개 미러 일시 빈응답) 재시도.
  while : ; do
    if COMPOSE_PARALLEL_LIMIT=1 $COMPOSE build ${NOCACHE:+--no-cache} "$svc"; then
      echo ">> $svc OK"; break
    fi
    if [ "$n" -ge "$MAX_TRY" ]; then
      echo "!!! $svc : $MAX_TRY회 실패"; FAILED="$FAILED $svc"; break
    fi
    wait=$((n*5)); echo "-- $svc 재시도 $n/$((MAX_TRY-1)) (미러 일시 실패 추정, ${wait}s 후) --"
    n=$((n+1)); sleep "$wait"
  done
done

if [ -n "$FAILED" ]; then
  echo; echo "빌드 실패:$FAILED"
  echo "→ 위 서비스만 다시:  bash scripts/rebuild.sh   (또는 개별:  $COMPOSE build <svc>)"
  echo "  계속 같은 패키지에서 막히면 그 로그를 붙여줘(그 버전이 진짜 미러에 없는 경우일 수 있음)."
  exit 1
fi

echo; echo "== 기동 =="
$COMPOSE up -d
$COMPOSE ps
echo "== health =="; sleep 3
curl -sS "http://localhost:${AGENT_PORT:-8500}/health" || true
echo
