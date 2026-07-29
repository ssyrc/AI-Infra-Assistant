#!/usr/bin/env bash
# ssh 실행 경로 진단. 폐쇄망 배포 호스트(202.20.183.30)에서 실행한다.
#
#   bash scripts/diag-ssh.sh <호스트이름> <계정> [커맨드]
#   예) bash scripts/diag-ssh.sh login07 yr9.choi myquota
#
# "손으로 IP를 넣으면 되는데 에이전트로는 안 된다"의 원인을 한 번에 가른다.
# 에이전트는 이름(login07)을 /etc/hosts에서 IP로 바꿔 접속하므로, 그 이름이 다른 서버로
# 풀리면 키가 등록되지 않은 서버에 붙어 인증 실패가 난다.
set -u

HOST="${1:-login07}"
USER_ID="${2:-}"
CMD="${3:-id}"
SVC="${SVC:-command-mcp}"
COMPOSE="docker compose -f docker-compose.dev.yml"

if [ -z "$USER_ID" ]; then
  echo "사용법: bash scripts/diag-ssh.sh <호스트이름> <계정> [커맨드]" >&2
  exit 2
fi

echo "=== 1. 컨테이너가 보는 호스트 매핑 (/etc/hosts.targets) ==="
$COMPOSE exec -T "$SVC" sh -c "grep -w '$HOST' /etc/hosts.targets || echo '  !! $HOST 항목 없음'"

IP="$($COMPOSE exec -T "$SVC" sh -c \
  "awk -v h='$HOST' '\$0 !~ /^#/ { for (i=2; i<=NF; i++) if (\$i == h) { print \$1; exit } }' /etc/hosts.targets" \
  | tr -d '\r' | tr -d '[:space:]')"
echo "    -> '$HOST' 는 '${IP:-(해석 실패)}' 로 풀립니다."

echo
echo "=== 2. ssh 키 파일 상태 ==="
$COMPOSE exec -T "$SVC" sh -c \
  'ls -l /root/.ssh/id_ed25519 2>&1; test -f /root/.ssh/id_ed25519 && echo "  -> 파일 OK" || echo "  !! 파일이 아님(도커가 빈 디렉토리를 만든 상태일 수 있음)"'

echo
echo "=== 3. 에이전트와 똑같은 방식으로 실행 (이름 -> IP 해석 결과로 접속) ==="
if [ -z "$IP" ]; then
  echo "  !! IP 해석 실패라 건너뜁니다. 배포 호스트 /etc/hosts에 '$HOST' 항목을 추가하세요."
else
  $COMPOSE exec -T "$SVC" ssh \
    -o BatchMode=yes -o PasswordAuthentication=no \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile=/root/.ssh/known_hosts_agent \
    -o ConnectTimeout=8 \
    -i /root/.ssh/id_ed25519 "root@$IP" "su - $USER_ID -c $(printf '%q' "$CMD")" < /dev/null
  echo "    (종료코드 $?)"
fi

echo
echo "=== 4. 게이트 서버(202.20.185.100)로 직접 — 3번과 다르면 이름 해석이 원인 ==="
$COMPOSE exec -T "$SVC" ssh \
  -o BatchMode=yes -o PasswordAuthentication=no \
  -o StrictHostKeyChecking=accept-new \
  -o UserKnownHostsFile=/root/.ssh/known_hosts_agent \
  -o ConnectTimeout=8 \
  -i /root/.ssh/id_ed25519 root@202.20.185.100 "su - $USER_ID -c $(printf '%q' "$CMD")" < /dev/null
echo "    (종료코드 $?)"

echo
echo "판정: 3번이 실패하고 4번이 성공하면 -> '$HOST'가 잘못된 IP($IP)로 풀리는 것이 원인입니다."
echo "      배포 호스트 /etc/hosts의 '$HOST' 항목을 202.20.185.100으로 고치거나,"
echo "      관리자 콘솔 설정의 scheduler_login_host를 올바른 이름으로 바꾸세요."
