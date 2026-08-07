#!/usr/bin/env bash
# 커밋 전 검증. **커밋 전에 반드시 이걸로 돌린다.**
#
# 왜 있나: `pytest ... | tail -2 && git commit` 로 묶어 돌리다 **파이프가 종료코드를 가려**
# 실패한 테스트를 그대로 푸시한 일이 두 번 있었다(#146, #152). `set -o pipefail` 없이
# 파이프를 쓰면 마지막 명령(tail)의 성공만 보게 된다.
#
# 이 스크립트는 파이프를 쓰지 않고, 하나라도 실패하면 0이 아닌 코드로 끝난다.
set -euo pipefail

cd "$(cd "$(dirname "$0")/.." && pwd)"

echo "== pytest =="
PYTHONPATH="shared:admin_console/backend" python -m pytest tests/ -q

echo
echo "== pyflakes =="
# 기존에 남아 있는 경고 하나(voc_query의 in_think)는 제외한다. 새 경고는 그대로 실패시킨다.
if out=$(python -m pyflakes agent_server shared mcp_servers admin_console/backend tests 2>&1); then
  :
fi
filtered=$(printf '%s\n' "$out" | grep -v "in_think" || true)
if [ -n "$filtered" ]; then
  echo "$filtered"
  echo "!! 새 lint 경고가 있습니다."
  exit 1
fi
echo "(경고 없음)"

echo
echo "== 셸 스크립트 문법 =="
for f in scripts/*.sh; do
  bash -n "$f" || { echo "!! 문법 오류: $f"; exit 1; }
done
echo "(전부 통과)"

echo
echo "✅ 검증 통과"
