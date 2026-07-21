#!/usr/bin/env bash
# 미러에 특정 패키지의 '어떤 버전이 있는지' 프록시 경유로 확인한다.
# (이 폐쇄망은 내부 미러도 반드시 프록시 202.20.187.241:3128 를 통해서 나간다.)
#
# 사용법:
#   bash scripts/debug-now.sh                    # 기본: asyncpg fastapi pydantic docling
#   bash scripts/debug-now.sh asyncpg==0.31.0    # 특정 패키지(및 버전 존재여부) 확인
#   bash scripts/debug-now.sh pkgA pkgB ...
# 출력을 그대로 복사해서 붙여주면 됨.  파일로:  bash scripts/debug-now.sh | tee debug-now.out

set -u
INDEX_BASE="${PIP_INDEX_URL:-http://repository.samsungds.net/repository/proxy-pypi-files.pythonhosted.org/simple}"
INDEX_BASE="${INDEX_BASE%/}"
PROXY="${BUILD_PROXY:-http://202.20.187.241:3128}"

# .env 값이 있으면 우선 사용
if [ -f .env ]; then
  v=$(grep -E '^PIP_INDEX_URL=' .env | tail -1 | cut -d= -f2-); [ -n "${v:-}" ] && INDEX_BASE="${v%/}"
  v=$(grep -E '^BUILD_PROXY=' .env | tail -1 | cut -d= -f2-); [ -n "${v:-}" ] && PROXY="$v"
fi

ARGS=("$@"); [ ${#ARGS[@]} -eq 0 ] && ARGS=(asyncpg fastapi pydantic docling)

echo "date      : $(date '+%Y-%m-%d %H:%M:%S')"
echo "index-url : $INDEX_BASE"
echo "proxy     : $PROXY   (내부 미러도 프록시 경유 필수)"

for spec in "${ARGS[@]}"; do
  pkg="${spec%%==*}"; want=""; [ "$spec" != "$pkg" ] && want="${spec##*==}"
  # PEP503 정규화(소문자, _/. -> -)
  norm=$(printf '%s' "$pkg" | tr 'A-Z_.' 'a-z--')
  url="$INDEX_BASE/$norm/"
  printf '\n========== %s ==========\n' "$spec"
  echo "URL: $url"
  code=$(http_proxy="$PROXY" https_proxy="$PROXY" \
         curl -sS -o /tmp/dbg.$$ -w '%{http_code}' "$url" 2>/tmp/dbgerr.$$)
  echo "HTTP=$code, 응답=$(wc -c </tmp/dbg.$$ 2>/dev/null)bytes"
  [ -s /tmp/dbgerr.$$ ] && echo "(curl에러) $(head -1 /tmp/dbgerr.$$)"
  # 파일명에서 버전 추출
  vers=$(grep -oiE "${norm//-/[-_]}-[0-9][0-9a-z.!+]*" /tmp/dbg.$$ 2>/dev/null \
         | sed -E "s/^.*-([0-9][0-9a-z.!+]*)$/\1/i" | sort -uV)
  cnt=$(printf '%s\n' "$vers" | grep -c . )
  echo "미러에 있는 버전 수: $cnt"
  if [ "$cnt" -gt 0 ]; then
    echo "최근 버전들:"; printf '%s\n' "$vers" | tail -12 | sed 's/^/   /'
    if [ -n "$want" ]; then
      if printf '%s\n' "$vers" | grep -qx "$want"; then
        echo ">>> 요청 $want : 미러에 있음 ✅"
      else
        echo ">>> 요청 $want : 미러에 없음 ❌  (위 목록 중 하나로 핀을 바꾸거나 vendor에 넣어야 함)"
      fi
    fi
  else
    echo "(버전 0개 = 미러가 이 패키지 인덱스를 안 줌. HTTP코드/응답크기 확인)"
  fi
done
rm -f /tmp/dbg.$$ /tmp/dbgerr.$$

cat <<'EOF'

------ 판정 ------
- 특정 버전이 '없음 ❌' 이면: requirements의 그 핀을 '있음'인 버전으로 바꾸거나,
  그 버전 휠을 vendor/에 넣어 오프라인 설치한다(아래).
- 버전이 '0개' 로 아예 안 나오면: 인덱스 URL(PIP_INDEX_URL)이 잘못됐거나 프록시가 막힌 것.
  HTTP 코드가 200이 아니면 프록시/방화벽, 200인데 0개면 이 URL이 simple 인덱스가 아님.

------ 미러에 없는 버전을 vendor로 쓰는 법 ------
  1) 인터넷 되는 곳(WSL 등)에서:  pip download '<pkg>==<ver>' -d vendor/    (의존성까지 받으려면 --no-deps 빼기)
  2) rsync로 서버에 반영 후:       bash scripts/rebuild.sh
  (Dockerfile은 requirements 설치 시 vendor/를 find-links로 함께 본다면 자동 사용 — 배선은 문의)
EOF
echo
echo "위 전체 출력을 그대로 복사해서 붙여줘."
