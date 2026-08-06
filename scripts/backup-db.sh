#!/usr/bin/env bash
# postgres 전체 백업. **반영 작업 전에 항상 먼저 돌린다.**
#
# 왜 있나: #141에서 compose가 postgres 컨테이너를 재생성하면서 설정·매뉴얼·VOC·등록 커맨드가
# 전부 사라졌다. 익명 볼륨이었기 때문인데(지금은 이름 있는 볼륨으로 고쳤다), 볼륨이 있어도
# 사람이 `down -v`를 치는 순간 똑같이 사라진다. 백업이 있으면 5분이면 되돌아온다.
#
# 사용법:
#   bash scripts/backup-db.sh                    # 기본 위치에 저장
#   bash scripts/backup-db.sh /path/to/dir       # 저장 위치 지정
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.dev.yml}"
PG_USER="${PG_USER:-agent}"
# 저장소 **밖**이 기본값이다 - rsync --delete가 저장소 안의 파일을 지운 사고가 있었다(#137).
OUT_DIR="${1:-${BACKUP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}}"

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! docker compose -f "$COMPOSE_FILE" ps --status running --services 2>/dev/null | grep -qx postgres; then
  echo "[backup] postgres 컨테이너가 떠 있지 않습니다. 먼저 기동하세요:" >&2
  echo "         docker compose -f $COMPOSE_FILE up -d postgres" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/pg-backup-$(date +%Y%m%d-%H%M%S).sql"

echo "[backup] pg_dumpall -> $OUT"
# 실패하면 반쪽짜리 파일을 남기지 않는다. 그게 있으면 복구할 때 그걸 믿고 쓴다.
if ! docker compose -f "$COMPOSE_FILE" exec -T postgres pg_dumpall -U "$PG_USER" > "$OUT"; then
  rm -f "$OUT"
  echo "[backup] 실패. 파일을 남기지 않았습니다." >&2
  exit 1
fi

SIZE=$(du -h "$OUT" | cut -f1)
# 빈 덤프(0줄)는 성공으로 보이지만 아무것도 못 되돌린다. 눈에 띄게 막는다.
if ! grep -q "CREATE DATABASE" "$OUT"; then
  echo "[backup] 경고: 덤프에 CREATE DATABASE가 없습니다($SIZE). DB가 비어 있을 수 있습니다." >&2
  exit 1
fi

echo "[backup] 완료: $OUT ($SIZE)"
echo "[backup] 되돌리기: bash scripts/restore-db.sh $OUT"

# 오래된 백업 정리(기본 14개 유지). 디스크가 차서 백업이 실패하면 백업이 없는 것과 같다.
KEEP="${BACKUP_KEEP:-14}"
ls -1t "$OUT_DIR"/pg-backup-*.sql 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
  echo "[backup] 오래된 백업 삭제: $old"
  rm -f "$old"
done
