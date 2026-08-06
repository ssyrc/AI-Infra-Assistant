#!/usr/bin/env bash
# [WSL에서 실행] 저장소를 게이트 서버로 rsync 한다.
#
# 왜 스크립트인가: 이 커맨드를 문서에 적어 두고 복사해 쓰다가 사고가 났다(#137).
# `--delete`는 **저장소에 없는 파일을 서버에서 지운다** - `.env`와 ssh 키가 그렇게 사라졌다.
# 제외 목록을 문서가 아니라 코드에 두면 빠뜨릴 수 없다.
#
# 사용법:
#   bash scripts/deploy-rsync.sh              # 실제 전송
#   bash scripts/deploy-rsync.sh --dry-run    # 무엇이 지워지는지 먼저 본다
set -euo pipefail

DEST="${DEST:-yr9.choi@202.20.185.100:/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/"

# 서버에만 있고 저장소에는 없는 것들. **여기에 없으면 --delete가 지운다.**
# 새로 서버에 상태를 두게 되면 반드시 이 목록에 추가할 것.
EXCLUDES=(
  --exclude '.env'            # 실제 vLLM 주소·비밀번호. 지워지면 서비스가 mock으로 돌아간다
  --exclude '.env.*'
  --exclude 'secrets/'        # ssh 개인키
  --exclude 'dist/'           # 이미지 tar (용량)
  --exclude '__pycache__/'
  --exclude '.git/'
  --exclude 'pg-backup-*.sql' # DB 백업이 저장소 안에 놓인 경우 (#141)
)

DRY=()
if [ "${1:-}" = "--dry-run" ]; then
  DRY=(--dry-run)
  echo "[deploy] --dry-run: 실제로 전송하지 않습니다."
fi

echo "[deploy] $SRC"
echo "[deploy]   -> $DEST"

# 무엇이 **지워지는지** 먼저 보여준다. 조용히 지우는 것이 #137의 본질이었다.
echo "[deploy] --delete로 서버에서 지워질 것:"
DELETED=$(rsync -az --delete --dry-run --out-format='%o %n' "${EXCLUDES[@]}" "$SRC" "$DEST" \
          | awk '$1=="del." {print "         " $2}')
if [ -z "$DELETED" ]; then
  echo "         (없음)"
else
  echo "$DELETED"
  if [ ${#DRY[@]} -eq 0 ]; then
    printf "[deploy] 위 파일이 서버에서 삭제됩니다. 계속할까요? [y/N] "
    read -r ans
    case "$ans" in [yY]*) ;; *) echo "[deploy] 중단했습니다."; exit 1 ;; esac
  fi
fi

rsync -avz --delete --progress "${DRY[@]}" "${EXCLUDES[@]}" "$SRC" "$DEST"
echo "[deploy] 완료."
