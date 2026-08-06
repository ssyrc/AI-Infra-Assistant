#!/usr/bin/env bash
# 사라진 postgres 데이터를 찾는다 (#141).
#
# 앞선 진단이 아무것도 못 찾은 이유가 둘 있었다. 둘 다 이 스크립트에서 고쳤다.
#   1) `docker run alpine`을 썼다 — **폐쇄망은 alpine을 pull하지 못한다.** 게다가 에러를
#      `2>/dev/null`로 가려서 "볼륨이 없다"와 "이미지를 못 받았다"가 구분되지 않았다.
#      -> 이미 로컬에 있는 postgres 이미지를 쓰고, 실패하면 그 사실을 그대로 찍는다.
#   2) `docker volume ls -f dangling=true`만 봤다 — 멈춰 있는 컨테이너에 붙어 있는 볼륨은
#      dangling이 아니라서 목록에 안 나온다. -> **볼륨 전체**를 훑는다.
#
# 읽기만 한다. 아무것도 지우거나 바꾸지 않는다.
set -uo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.dev.yml}"

# compose 파일이 있는 곳을 **찾아서** 들어간다.
# 예전에는 `cd "$(dirname "$0")/.."` 였는데, 스크립트를 저장소 루트에 복사해 두고 실행하면
# 한 단계 위(`05_halo/`)로 올라가 compose 파일을 못 찾았다. 그래서 postgres가 멀쩡히 떠 있는데도
# "컨테이너를 찾지 못했습니다"라고 찍혔다 - 진단이 거짓말을 하면 없느니만 못하다.
_here=$(cd "$(dirname "$0")" && pwd)
for _cand in "$_here" "$_here/.." "$PWD" "$PWD/.."; do
  if [ -f "$_cand/$COMPOSE_FILE" ]; then cd "$_cand" && break; fi
done
if [ ! -f "$COMPOSE_FILE" ]; then
  echo "[진단] $COMPOSE_FILE 을 찾지 못했습니다. 저장소 루트에서 실행하세요:" >&2
  echo "       cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant && bash scripts/find-lost-db.sh" >&2
  exit 1
fi
echo "[진단] 작업 디렉토리: $PWD"

hr() { printf '\n===== %s =====\n' "$1"; }

hr "1) 지금 postgres 컨테이너가 쓰는 볼륨"
PG_CID=$(docker compose -f "$COMPOSE_FILE" ps -aq postgres 2>/dev/null | head -1)
if [ -n "$PG_CID" ]; then
  docker inspect -f '컨테이너: {{.Name}} ({{.State.Status}}){{range .Mounts}}
  [{{.Type}}] {{if .Name}}{{.Name}}{{else}}{{.Source}}{{end}} -> {{.Destination}}{{end}}' "$PG_CID"
  echo
  echo "  ※ 위에 이름이 64자리 16진수면 **익명 볼륨**입니다(이번 사고의 원인)."
  echo "     'pg_data_dev'면 이미 고친 코드로 떠 있는 것입니다."
else
  echo "postgres 컨테이너를 찾지 못했습니다."
fi

hr "2) 볼륨 전체 (dangling 조건 없이)"
docker volume ls

hr "3) 컨테이너 전체 (죽은 것 포함)"
docker ps -a --format '{{.Names}}\t{{.Status}}\t{{.Image}}'

hr "4) 어떤 볼륨이 postgres 데이터인가"
# **로컬에 이미 있는** 이미지를 쓴다. pull이 필요한 이미지를 쓰면 폐쇄망에서 전부 실패한다.
PGIMG=""
[ -n "$PG_CID" ] && PGIMG=$(docker inspect -f '{{.Config.Image}}' "$PG_CID" 2>/dev/null)
if [ -z "$PGIMG" ]; then
  PGIMG=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep -E 'pgvector|postgres' | head -1)
fi

if [ -z "$PGIMG" ]; then
  echo "로컬에 postgres 계열 이미지가 없습니다. 5)의 호스트 경로 방식으로 확인하세요."
else
  echo "사용할 이미지(로컬): $PGIMG"
  probe=$(docker run --rm --entrypoint sh "$PGIMG" -c 'echo ok' 2>&1)
  if [ "$probe" != "ok" ]; then
    echo "이 이미지로 컨테이너를 띄우지 못했습니다. 원문:"
    echo "  $probe"
    echo "5)의 호스트 경로 방식으로 확인하세요."
  else
    found=0
    for v in $(docker volume ls -q); do
      ver=$(docker run --rm --entrypoint cat -v "$v":/v "$PGIMG" /v/PG_VERSION 2>/dev/null)
      case "$ver" in
        1[0-9]*)
          size=$(docker run --rm --entrypoint du -v "$v":/v "$PGIMG" -sh /v 2>/dev/null | cut -f1)
          echo "  [PG 데이터] $v   PG_VERSION=$ver   크기=${size:-?}"
          found=$((found + 1)) ;;
      esac
    done
    [ "$found" -eq 0 ] && echo "  postgres 데이터를 담은 볼륨이 하나도 없습니다."
  fi
fi

hr "5) 볼륨의 호스트 경로 (도커로 안 될 때 직접 확인)"
for v in $(docker volume ls -q); do
  mp=$(docker volume inspect -f '{{.Mountpoint}}' "$v" 2>/dev/null)
  echo "  $v"
  echo "      $mp"
done
echo
echo "  root 권한이 있으면 이렇게 직접 볼 수 있습니다(pull 불필요):"
echo "      sudo ls -la <위 경로>/ | head"
echo "      sudo cat <위 경로>/PG_VERSION"

hr "6) 지금 떠 있는 DB에 실제로 데이터가 있는가"
if docker compose -f "$COMPOSE_FILE" ps --status running --services 2>/dev/null | grep -qx postgres; then
  # 표 이름은 shared/migrations.py의 CREATE TABLE과 맞춰야 한다.
  # (manuals/voc_items로 잘못 적었다가 진단이 또 헛돌 뻔했다 - 실제로는 manual_files/voc_records)
  for pair in "platform_config:platform_settings" "manual_db:manual_files" \
              "manual_db:manual_chunks" "voc_db:voc_records" \
              "command_db:execution_commands" "command_db:job_logs"; do
    db="${pair%%:*}"; tbl="${pair##*:}"
    n=$(docker compose -f "$COMPOSE_FILE" exec -T postgres \
        psql -U agent -d "$db" -tAc "select count(*) from $tbl" 2>&1 | tr -d '\r' | head -1)
    echo "  $db.$tbl = $n"
  done
else
  echo "postgres가 running이 아닙니다. 먼저: docker compose -f $COMPOSE_FILE up -d postgres"
fi

echo
echo "이 출력을 통째로 보내주세요."
