#!/usr/bin/env bash
# 커맨드 실행이 **어디서** 느린지 초 단위로 쪼개 본다. 폐쇄망 배포 호스트(202.20.183.30)에서 실행.
#
#   bash scripts/bench-exec.sh <계정> [커맨드] [호스트IP]
#   예) bash scripts/bench-exec.sh yr9.choi myquota
#
# 한 번의 커맨드 실행 시간은 세 덩어리로 나뉜다.
#   (1) 접속        TCP + 키교환. 다중화 마스터가 서 있으면 0에 가깝다.
#   (2) su - 로그인 셸  원격 계정의 프로필(/etc/profile, .bashrc, 모듈 초기화 …)을 매번 읽는다.
#                    HPC 로그인 노드에서는 이게 수 초일 수 있고, 다중화로도 줄지 않는다.
#   (3) 커맨드 자체
# 아래 4개를 재면 (1)(2)(3)이 각각 얼마인지 바로 나온다. 추측하지 말고 이 숫자를 보낸다.
set -u

USER_ID="${1:-}"
CMD="${2:-true}"
HOST="${3:-}"
SVC="${SVC:-execution-mcp}"
COMPOSE="docker compose -f docker-compose.dev.yml"

if [ -z "$USER_ID" ]; then
  echo "사용법: bash scripts/bench-exec.sh <계정> [커맨드] [호스트IP]" >&2
  exit 2
fi

if [ -z "$HOST" ]; then
  HOST="$($COMPOSE exec -T "$SVC" sh -c \
    'python -c "import os,sys;sys.path.insert(0,\"/app/shared\");from config_store import get_config;import asyncio;print(asyncio.run(get_config(\"execution_host\",\"202.20.185.100\")))"' \
    2>/dev/null | tr -d '\r[:space:]')"
  HOST="${HOST:-202.20.185.100}"
fi
echo "대상: root@$HOST · 실행 계정: $USER_ID · 커맨드: $CMD"

OPTS="-o BatchMode=yes -o PasswordAuthentication=no -o StrictHostKeyChecking=accept-new \
-o UserKnownHostsFile=/root/.ssh/known_hosts_agent -o ConnectTimeout=8 \
-o ControlMaster=auto -o ControlPath=/tmp/.ssh-mux/bench-%C -o ControlPersist=300 \
-i /root/.ssh/id_ed25519"

run() {                     # run <설명> <원격명령>
  local label="$1" remote="$2" start end
  start=$(date +%s.%N)
  $COMPOSE exec -T "$SVC" sh -c \
    "mkdir -p /tmp/.ssh-mux && ssh $OPTS root@$HOST $(printf '%q' "$remote") >/dev/null 2>&1 </dev/null"
  local code=$?
  end=$(date +%s.%N)
  printf '  %-34s %6.2f초  (종료코드 %d)\n' "$label" "$(echo "$end - $start" | bc)" "$code"
}

echo
echo "=== 1회차: 마스터 연결이 없는 상태(첫 커맨드와 같은 조건) ==="
$COMPOSE exec -T "$SVC" sh -c "rm -f /tmp/.ssh-mux/bench-* 2>/dev/null" >/dev/null 2>&1
run "접속만 (true)"                       "true"

echo
echo "=== 2회차 이후: 마스터 재사용 ==="
run "접속만 (true)"                       "true"
run "su - 로그인 셸만 (su - … -c true)"    "su - $USER_ID -c true"
run "실제 커맨드"                          "su - $USER_ID -c $(printf '%q' "$CMD")"

$COMPOSE exec -T "$SVC" sh -c "ssh $OPTS -O exit root@$HOST" >/dev/null 2>&1

cat <<'EOF'

읽는 법
  · 1회차 "접속만"이 크고 2회차가 작다 → 다중화는 동작 중. 기동 로그에서 마스터 예열이
    성공했는지 확인한다(execution-mcp 로그의 "ssh 다중화 마스터 준비 완료").
  · 2회차 "접속만"도 크다 → 마스터가 유지되지 않는다. 방화벽이 유휴 연결을 끊는지 확인
    (.env의 SSH_ALIVE_INTERVAL / SSH_CONTROL_PERSIST).
  · "su - 로그인 셸만"이 크다 → 원격 계정 프로필이 무겁다. 이건 우리 쪽에서 줄일 수 없고,
    커맨드를 여러 번 부를수록 그대로 곱해진다(= 도구 호출 횟수를 줄여야 한다).
  · "실제 커맨드"만 크다 → 커맨드 자체가 느린 것이다(GPFS 쿼터 조회 등). 정상이다.
EOF
