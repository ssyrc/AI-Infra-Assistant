#!/usr/bin/env bash
# backup-db.sh가 만든 덤프를 되돌린다.
#
# 사용법:
#   bash scripts/restore-db.sh /home/gpu1/yr9.choi/05_halo/pg-backup-20260806-1530.sql
#
# pg_dumpall 덤프는 CREATE DATABASE와 데이터를 함께 담고 있다. 이미 같은 이름의 DB가 있으면
# "already exists" 오류가 줄줄이 나면서 **일부만 들어간다** - 그 상태가 제일 나쁘다.
# 그래서 기본은 "빈 클러스터에 붓는" 것이고, 기존 DB를 지우는 것은 명시적으로 시켜야 한다.
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.dev.yml}"
PG_USER="${PG_USER:-agent}"
DUMP="${1:-}"

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ -z "$DUMP" ] || [ ! -f "$DUMP" ]; then
  echo "사용법: bash scripts/restore-db.sh <덤프파일.sql>" >&2
  echo "가능한 백업:" >&2
  ls -1t ../pg-backup-*.sql 2>/dev/null | head -10 >&2 || echo "  (없음)" >&2
  exit 1
fi

if ! grep -q "CREATE DATABASE" "$DUMP"; then
  echo "[restore] '$DUMP'는 pg_dumpall 덤프가 아닌 것 같습니다(CREATE DATABASE 없음)." >&2
  exit 1
fi

psql_run() { docker compose -f "$COMPOSE_FILE" exec -T postgres psql -U "$PG_USER" -d postgres "$@"; }

if ! docker compose -f "$COMPOSE_FILE" ps --status running --services 2>/dev/null | grep -qx postgres; then
  echo "[restore] postgres가 떠 있지 않습니다: docker compose -f $COMPOSE_FILE up -d postgres" >&2
  exit 1
fi

EXISTING=$(psql_run -tAc \
  "select string_agg(datname, ' ') from pg_database where datname in
   ('platform_config','manual_db','voc_db','command_db','system_db','agent_sessions_db','memory_db')" \
  | tr -d '\r')

if [ -n "${EXISTING// /}" ]; then
  if [ "${DROP_EXISTING:-}" != "yes" ]; then
    cat >&2 <<MSG
[restore] 이미 DB가 있습니다: $EXISTING

  그대로 부으면 "already exists" 오류가 나면서 **일부만** 들어갑니다.
  기존 것을 버리고 덤프로 완전히 되돌리려면:

      DROP_EXISTING=yes bash scripts/restore-db.sh $DUMP

  지금 들어 있는 내용을 먼저 남겨 두려면:

      bash scripts/backup-db.sh
MSG
    exit 1
  fi
  echo "[restore] 기존 DB를 삭제합니다: $EXISTING"
  for db in $EXISTING; do
    psql_run -c "DROP DATABASE IF EXISTS \"$db\" WITH (FORCE);" >/dev/null
  done
fi

echo "[restore] $DUMP 적용 중…"
docker compose -f "$COMPOSE_FILE" exec -T postgres psql -U "$PG_USER" -d postgres < "$DUMP"

echo "[restore] 확인:"
psql_run -c "select datname from pg_database where datistemplate = false order by 1;"
echo "[restore] 완료. 서비스를 다시 시작하세요: bash scripts/restart-mounted.sh"
