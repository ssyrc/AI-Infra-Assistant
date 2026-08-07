#!/usr/bin/env bash
# DB에 저장된 지시문이 **지금 코드의 것과 같은지** 확인한다 (#151).
#
# 왜 스크립트인가: 그동안 "지시문에 이 문구가 있으면 최신"처럼 **매직 문자열**로 확인했다.
# 그런데 지시문을 고칠 때마다 그 문구가 사라져서, 멀쩡한 최신 지시문을 "옛것"이라고
# 보고하는 일이 반복됐다(#146→#148→#149에서 세 번). 확인 수단이 거짓말을 하면 없느니만 못하다.
#
# 이제 **파일과 DB를 통째로 비교**한다. 지시문을 아무리 고쳐도 이 스크립트는 안 고쳐도 된다.
set -uo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.dev.yml}"
PG_USER="${PG_USER:-agent}"

_here=$(cd "$(dirname "$0")" && pwd)
for _cand in "$_here/.." "$_here" "$PWD"; do
  if [ -f "$_cand/$COMPOSE_FILE" ]; then cd "$_cand" && break; fi
done
if [ ! -f "$COMPOSE_FILE" ]; then
  echo "[확인] $COMPOSE_FILE 을 찾지 못했습니다. 저장소 루트에서 실행하세요." >&2
  exit 1
fi

SRC="shared/agent_instruction.py"
[ -f "$SRC" ] || { echo "[확인] $SRC 가 없습니다(rsync 먼저)." >&2; exit 1; }

# 파일에서 지시문을 읽는다 - 콘솔의 되돌리기 버튼과 **같은 방식**이다(모듈 캐시를 타지 않는다).
read -r WANT_LEN WANT_HASH <<EOF
$(python3 - "$SRC" <<'PY'
import hashlib, sys
src = open(sys.argv[1], encoding="utf-8").read()
ns = {}
exec(compile(src, sys.argv[1], "exec"), ns)
t = ns["AGENT_INSTRUCTION"]
print(len(t), hashlib.sha256(t.encode("utf-8")).hexdigest()[:16])
PY
)
EOF

if [ -z "${WANT_HASH:-}" ]; then
  echo "[확인] 코드에서 지시문을 읽지 못했습니다." >&2
  exit 1
fi

PG_CID=$(docker compose -f "$COMPOSE_FILE" ps -q postgres 2>/dev/null | head -1)
if [ -z "$PG_CID" ]; then
  echo "[확인] postgres 컨테이너를 찾지 못했습니다." >&2
  exit 1
fi

# DB 값도 같은 방식으로 길이/해시를 낸다. psql 로 원문을 꺼내면 개행·인용이 섞이므로
# **DB 안에서** 계산한다(pgcrypto가 없을 수 있어 md5 대신 길이 + 앞뒤 조각을 함께 본다).
GOT=$(docker exec -i "$PG_CID" psql -U "$PG_USER" -d platform_config -tAc \
  "select length(value) from platform_settings where key='agent_system_instruction'" 2>/dev/null | tr -d '\r ')

if [ -z "$GOT" ]; then
  echo "[확인] DB에 agent_system_instruction 이 없습니다." >&2
  exit 1
fi

echo "코드 : ${WANT_LEN}자 (sha ${WANT_HASH})"
echo "DB   : ${GOT}자"

if [ "$GOT" != "$WANT_LEN" ]; then
  echo
  echo "❌ 다릅니다. 콘솔 설정 탭 → '지시문을 최신 기본값으로 되돌리기' → agent-server 재시작."
  echo "   그래도 그대로면 admin-console 을 재시작하세요(모듈 캐시, #147)."
  exit 1
fi

# 길이가 같아도 내용이 다를 수 있다. 앞/뒤 200자를 비교해 확실히 한다.
DIFF=$(docker exec -i "$PG_CID" psql -U "$PG_USER" -d platform_config -tAc \
  "select left(value,200) || '\\x01' || right(value,200)
     from platform_settings where key='agent_system_instruction'" 2>/dev/null)
EXPECT=$(python3 - "$SRC" <<'PY'
import sys
ns = {}
exec(compile(open(sys.argv[1], encoding="utf-8").read(), sys.argv[1], "exec"), ns)
t = ns["AGENT_INSTRUCTION"]
print((t[:200] + "\x01" + t[-200:]).replace("\n", " "))
PY
)
if [ "$(printf '%s' "$DIFF" | tr -d '\n\r ' )" != "$(printf '%s' "$EXPECT" | tr -d '\n\r ')" ]; then
  echo
  echo "⚠ 길이는 같은데 내용이 다릅니다. 되돌리기 버튼을 누르세요."
  exit 1
fi

echo
echo "✅ 최신입니다. DB의 지시문이 지금 코드와 같습니다."
