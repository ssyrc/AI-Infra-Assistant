#!/usr/bin/env bash
# "curl은 되는데 pip(빌드 컨테이너)만 안 되는" 원인을 가른다.
#   A) 미러가 주는 Content-Type 확인 (pip는 text/html 이 아니면 인덱스를 무시 -> from versions: none)
#   B) 빌드와 '같은 컨테이너'에서 프록시 경유로 미러에 도달하는지 (urllib)
#   C) 같은 컨테이너에서 실제 pip 로 인덱스가 파싱되는지
# 사용법:  bash scripts/debug-net.sh   (출력을 그대로 붙여줘)
set -u

# .env 우선
INDEX_BASE="http://repository.samsungds.net/repository/proxy-pypi-files.pythonhosted.org/simple"
PROXY="http://202.20.187.241:3128"
REG="repository.samsungds.net/proxy-docker-registry-1.docker.io"
if [ -f .env ]; then
  v=$(grep -E '^PIP_INDEX_URL=' .env | tail -1 | cut -d= -f2-); [ -n "${v:-}" ] && INDEX_BASE="${v%/}"
  v=$(grep -E '^BUILD_PROXY=' .env | tail -1 | cut -d= -f2-); [ -n "${v:-}" ] && PROXY="$v"
  v=$(grep -E '^REGISTRY_DOCKERHUB=' .env | tail -1 | cut -d= -f2-); [ -n "${v:-}" ] && REG="$v"
fi
INDEX_BASE="${INDEX_BASE%/}"
URL="$INDEX_BASE/fastapi/"
IMG="$REG/python:3.11-slim-bullseye"

echo "index : $INDEX_BASE"
echo "proxy : $PROXY"
echo "image : $IMG"

echo
echo "===== A) 호스트에서 프록시 경유 응답 헤더 (Content-Type 중요) ====="
http_proxy="$PROXY" https_proxy="$PROXY" curl -sSI "$URL" 2>&1 | grep -iE 'HTTP/|content-type|content-length|location|server' || echo "(헤더 못 받음)"

echo
echo "===== B) 빌드와 같은 컨테이너에서 프록시 경유 접근 (urllib) ====="
docker run --rm \
  -e http_proxy="$PROXY" -e https_proxy="$PROXY" -e no_proxy=localhost,127.0.0.1 \
  "$IMG" \
  python -c "
import urllib.request as u
req=u.Request('$URL', headers={'Accept':'text/html'})
try:
    r=u.urlopen(req, timeout=20); b=r.read()
    print('HTTP', r.status, 'bytes', len(b))
    print('Content-Type:', r.headers.get('Content-Type'))
    print('앞부분:', b[:160])
except Exception as e:
    print('ERROR', repr(e))
" 2>&1

echo
echo "===== C) 같은 컨테이너에서 실제 pip(22.1.2) 로 인덱스 조회 ====="
docker run --rm \
  -e http_proxy="$PROXY" -e https_proxy="$PROXY" -e no_proxy=localhost,127.0.0.1 \
  -e PIP_INDEX_URL="$INDEX_BASE" -e PIP_TRUSTED_HOST="$(echo "$INDEX_BASE" | awk -F/ '{print $3}')" \
  -v "$PWD/vendor:/tmp/vendor:ro" \
  "$IMG" \
  sh -c "pip install --no-index --no-cache-dir /tmp/vendor/pip-*.whl >/dev/null 2>&1; pip --version; pip download --no-deps -d /tmp/x fastapi==0.115.8 2>&1 | tail -15" 2>&1

echo
cat <<'EOF'
------ 판정 ------
A) Content-Type 이 text/html 이 아니면(예: application/octet-stream, text/plain) → pip가 인덱스를
   무시해서 'from versions: none'. 미러 관리자에게 simple 인덱스 Content-Type를 text/html 로 요청.
B) 여기서 ERROR/타임아웃 → 빌드 컨테이너가 프록시(202.20.187.241:3128)에 도달 못 함(도커 빌드 네트워크).
   B는 되는데 C만 실패 → pip 설정 문제(대개 A의 Content-Type).
C) 'Saved .../fastapi-...whl' 나오면 컨테이너에서 pip가 미러로 다운로드 성공 = 진짜 원인 규명됨.
EOF
echo "위 A/B/C 전체 출력을 붙여줘."
