#!/usr/bin/env bash
# 지금 막힌 지점(pip "from versions: none")을 진단한다.
# 사용법:  bash scripts/debug-now.sh   (그리고 전체 출력을 그대로 복사해서 붙여주면 됨)
#
# 손으로 커맨드 칠 필요 없이 이 스크립트 하나만 실행하면 된다.
# 결과를 파일로 남기려면:  bash scripts/debug-now.sh | tee debug-now.out

set -u

MIRROR_HOST="repository.samsungds.net"
INDEX_BASE="http://repository.samsungds.net/repository/proxy-pypi-files.pythonhosted.org/simple"
PROXY="${BUILD_PROXY:-http://202.20.187.241:3128}"
PKGS="asyncpg fastapi pydantic"

line() { printf '\n========== %s ==========\n' "$1"; }

line "0) 환경/설정"
echo "date        : $(date '+%Y-%m-%d %H:%M:%S')"
echo "index-url   : $INDEX_BASE"
echo "proxy       : $PROXY"
echo "env http_proxy=${http_proxy:-} https_proxy=${https_proxy:-} no_proxy=${no_proxy:-}"
echo "--- pip.conf (있으면) ---"
for c in /etc/pip.conf "$HOME/.pip/pip.conf" "$HOME/.config/pip/pip.conf"; do
  [ -f "$c" ] && { echo "[$c]"; cat "$c"; }
done
echo "--- .env의 PIP/PROXY 관련 ---"
[ -f .env ] && grep -E 'PIP_INDEX_URL|PIP_TRUSTED_HOST|BUILD_PROXY|NO_PROXY' .env 2>/dev/null || echo "(.env 없음 또는 해당 값 없음)"

# 패키지별로 프록시 통함 vs 직접 접근 결과 비교
for pkg in $PKGS; do
  url="$INDEX_BASE/$pkg/"
  line "패키지: $pkg"
  echo "URL: $url"

  echo "--- ① 프록시 통해서 ---"
  code=$(http_proxy="$PROXY" https_proxy="$PROXY" curl -sS -o /tmp/dbg_proxy.$$ -w '%{http_code}' "$url" 2>/tmp/dbg_proxy_err.$$)
  n=$(grep -c -iE 'href=|'"$pkg"'-' /tmp/dbg_proxy.$$ 2>/dev/null)
  echo "HTTP=$code, 파일링크수=$n, 응답크기=$(wc -c </tmp/dbg_proxy.$$ 2>/dev/null)bytes"
  [ -s /tmp/dbg_proxy_err.$$ ] && { echo "(에러) $(head -1 /tmp/dbg_proxy_err.$$)"; }
  grep -oiE "$pkg-[0-9][^\"<> ]*\.(whl|tar\.gz)" /tmp/dbg_proxy.$$ 2>/dev/null | sort -u | tail -5

  echo "--- ② 프록시 없이 직접 ---"
  code=$(curl -sS --noproxy '*' -o /tmp/dbg_direct.$$ -w '%{http_code}' "$url" 2>/tmp/dbg_direct_err.$$)
  n=$(grep -c -iE 'href=|'"$pkg"'-' /tmp/dbg_direct.$$ 2>/dev/null)
  echo "HTTP=$code, 파일링크수=$n, 응답크기=$(wc -c </tmp/dbg_direct.$$ 2>/dev/null)bytes"
  [ -s /tmp/dbg_direct_err.$$ ] && { echo "(에러) $(head -1 /tmp/dbg_direct_err.$$)"; }
  grep -oiE "$pkg-[0-9][^\"<> ]*\.(whl|tar\.gz)" /tmp/dbg_direct.$$ 2>/dev/null | sort -u | tail -5
done
rm -f /tmp/dbg_proxy.$$ /tmp/dbg_direct.$$ /tmp/dbg_proxy_err.$$ /tmp/dbg_direct_err.$$

line "판정 가이드"
cat <<'EOF'
- ②(직접)만 파일링크가 뜨고 ①(프록시)은 0개/빔  → 내부 미러를 프록시로 태워서 깨짐.
    해결: .env 의 NO_PROXY 에 repository.samsungds.net 추가 후 재빌드
          (scripts/set-noproxy-direct.sh 실행하면 자동으로 바꿔줌)
- ①②(둘 다) 파일링크 뜸  → 프록시 문제 아님. 일시장애 → 재빌드 재시도.
- ①②(둘 다) 0개/빔  → 이 URL이 files 프록시라 simple 인덱스가 아님.
    미러 관리자에게 'pypi.org simple proxy' repo URL 문의.
EOF
echo
echo "위 전체 출력을 그대로 복사해서 붙여줘."
