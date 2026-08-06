#!/usr/bin/env bash
# 볼륨 하나를 임시 postgres로 열어 **무엇이 들어 있는지** 보여준다 (#141).
#
#   bash scripts/inspect-pg-volume.sh 4f7c8f2ee8fe...
#
# 익명 볼륨이 여러 개 남아 있을 때 어느 것이 진짜 데이터인지 고르기 위한 것이다.
# 행 수와 **가장 최근 갱신 시각**을 함께 찍는다 - 크기만으로는 못 고른다(빈 클러스터도 130MB다).
#
# 쓰기를 최소화한다: 임시 컨테이너는 포트를 열지 않고, 확인이 끝나면 지운다.
# postgres가 뜨면서 WAL 복구로 볼륨을 건드릴 수는 있는데(정상 동작이다), 데이터를 지우지는
# 않는다. 그래도 불안하면 COPY_FIRST=yes 로 복사본을 만들어 검사한다.
set -uo pipefail

VOL="${1:-}"
NAME="pg-inspect-$$"

if [ -z "$VOL" ]; then
  echo "사용법: bash scripts/inspect-pg-volume.sh <볼륨이름>" >&2
  echo "볼륨 목록: bash scripts/find-lost-db.sh" >&2
  exit 1
fi

if ! docker volume inspect "$VOL" >/dev/null 2>&1; then
  echo "그런 볼륨이 없습니다: $VOL" >&2
  exit 1
fi

# **로컬에 있는** pgvector 이미지를 쓴다(폐쇄망은 pull하지 못한다).
PGIMG=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep -E 'pgvector' | head -1)
if [ -z "$PGIMG" ]; then
  echo "로컬에 pgvector 이미지가 없습니다." >&2
  exit 1
fi
echo "[검사] 볼륨=$VOL  이미지=$PGIMG"

TARGET="$VOL"
if [ "${COPY_FIRST:-}" = "yes" ]; then
  TARGET="pg-inspect-copy-$$"
  echo "[검사] 원본을 건드리지 않도록 복사본을 만듭니다: $TARGET"
  docker volume create "$TARGET" >/dev/null
  docker run --rm -v "$VOL":/from -v "$TARGET":/to --entrypoint sh "$PGIMG" \
    -c 'cp -a /from/. /to/' || { echo "복사 실패" >&2; exit 1; }
fi

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1
  [ "${COPY_FIRST:-}" = "yes" ] && docker volume rm "$TARGET" >/dev/null 2>&1
  return 0
}
trap cleanup EXIT

# 포트를 열지 않는다(운영 중인 8507과 부딪히지 않게). 접근은 docker exec으로만.
docker run -d --name "$NAME" -v "$TARGET":/var/lib/postgresql/data \
  -e POSTGRES_PASSWORD=devpass "$PGIMG" >/dev/null 2>&1

echo -n "[검사] 기동 대기"
for _ in $(seq 1 30); do
  if docker exec "$NAME" pg_isready -U agent -d postgres >/dev/null 2>&1; then break; fi
  printf '.'; sleep 1
done
echo

if ! docker exec "$NAME" pg_isready -U agent -d postgres >/dev/null 2>&1; then
  echo "[검사] postgres가 뜨지 않았습니다. 로그 마지막 20줄:"
  docker logs --tail=20 "$NAME" 2>&1 | sed 's/^/    /'
  exit 1
fi

echo
echo "--- DB 목록 ---"
docker exec "$NAME" psql -U agent -d postgres -tAc \
  "select datname from pg_database where datistemplate=false order by 1" 2>&1 | sed 's/^/    /'

echo
echo "--- 표별 행 수 / 마지막 갱신 ---"
for pair in "platform_config:platform_settings" "manual_db:manual_files" \
            "manual_db:manual_chunks" "voc_db:voc_records" \
            "command_db:execution_commands" "command_db:job_logs"; do
  db="${pair%%:*}"; tbl="${pair##*:}"
  n=$(docker exec "$NAME" psql -U agent -d "$db" -tAc \
      "select count(*) from $tbl" 2>/dev/null | tr -d '\r')
  if [ -z "$n" ]; then
    printf '    %-34s (표 없음)\n' "$db.$tbl"
    continue
  fi
  # 마지막 갱신 시각까지 찍는다 - **어느 볼륨이 최신인지는 크기가 아니라 이걸로** 고른다.
  # 시각 컬럼 이름이 표마다 다르다(updated_at / created_at / uploaded_at). 이름을 박아 두면
  # 조용히 빈 값이 나오므로 카탈로그에서 찾아 쓴다.
  ts=""
  col=$(docker exec "$NAME" psql -U agent -d "$db" -tAc \
        "select column_name from information_schema.columns
          where table_name='$tbl' and data_type like 'timestamp%'
          order by case column_name
            when 'updated_at' then 1 when 'uploaded_at' then 2
            when 'created_at' then 3 else 4 end limit 1" 2>/dev/null | tr -d '\r ')
  [ -n "$col" ] && ts=$(docker exec "$NAME" psql -U agent -d "$db" -tAc \
       "select coalesce(max($col)::text,'') from $tbl" 2>/dev/null | tr -d '\r')
  [ -n "$ts" ] && ts="$col=$ts"
  printf '    %-34s %8s 행   %s\n' "$db.$tbl" "$n" "$ts"
done

echo
echo "--- 등록된 커맨드 이름 (있으면) ---"
docker exec "$NAME" psql -U agent -d command_db -tAc \
  "select title from execution_commands order by title" 2>/dev/null | sed 's/^/    /' | head -20

echo
echo "--- 매뉴얼 파일 이름 (있으면) ---"
docker exec "$NAME" psql -U agent -d manual_db -tAc \
  "select filename from manual_files order by id desc limit 10" 2>/dev/null | sed 's/^/    /'

echo
echo "[검사] 끝. 임시 컨테이너를 정리합니다(원본 볼륨은 그대로 둡니다)."
