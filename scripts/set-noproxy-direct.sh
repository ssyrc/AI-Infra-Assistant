#!/usr/bin/env bash
# STEP 2-A 자동 적용: 내부 미러(repository.samsungds.net)를 외부 프록시 없이 '직접' 접근하도록
# .env 의 NO_PROXY 를 바꾼다. (DEBUG-NOW STEP1에서 ②직접만 성공했을 때 실행)
# 사용법:  bash scripts/set-noproxy-direct.sh
set -eu

MIRROR_HOST="repository.samsungds.net"
[ -f .env ] || { echo ".env 가 없다. 먼저: cp .env.example .env"; exit 1; }

if grep -q "$MIRROR_HOST" <(grep '^NO_PROXY=' .env 2>/dev/null); then
  echo "이미 NO_PROXY에 $MIRROR_HOST 있음. 변경 없음."
else
  if grep -q '^NO_PROXY=' .env; then
    sed -i "s#^NO_PROXY=.*#NO_PROXY=$MIRROR_HOST,localhost,127.0.0.1#" .env
  else
    printf 'NO_PROXY=%s,localhost,127.0.0.1\n' "$MIRROR_HOST" >> .env
  fi
  echo "적용됨:"
fi
grep '^NO_PROXY=' .env
echo
echo "이제 재빌드:  bash scripts/rebuild.sh"
