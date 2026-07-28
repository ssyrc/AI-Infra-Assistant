# 지금 할 일

## 지금 당장

### 1) System MCP "disabled by admin" — db-init을 안 돌렸을 가능성이 높음

로그의 `"reason": "disabled by admin"`은 `system_whitelist_state.enabled`가 실제로 false일 때만
나온다(콘솔에서 켠 게 반영이 안 된 것). 최근 커밋(host_mode 컬럼 추가)에서 관리자 콘솔의
PATCH가 이제 `host_mode` 컬럼도 같이 조회하는데, **이 컬럼이 없는 상태**(=db-init을 안 돌린
상태)면 콘솔에서 스위치를 켜는 API 호출 자체가 서버 에러로 조용히 실패해서 "켰는데 안 켜진 것
처럼" 보일 수 있다. 확인/조치:

```bash
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant log -1 --oneline   # 최신 커밋 받았는지 확인
```
서버에 반영 후:
```bash
docker compose -f docker-compose.dev.yml run --rm db-init
```
이 출력에 에러 없이 끝나는지 확인(특히 `system_db`쪽 마이그레이션). 그다음:
```bash
docker compose -f docker-compose.dev.yml restart admin-console system-mcp
```
admin_console System MCP 탭에서 `disk_free`를 다시 껐다 켜고 저장 → System MCP 재시작 버튼
클릭 → 다시 질문해서 실행 로그에 `success`로 찍히는지 확인. 그래도 `blocked`면
`docker compose -f docker-compose.dev.yml logs db-init --tail 80`을 보내달라(마이그레이션
자체가 에러로 실패했을 가능성 확인 필요).

### 2) "카탈로그에만 있고 화이트리스트엔 없는 커맨드도 실행 가능하게" — 설계 확인 필요

지금 커맨드 카탈로그(Command MCP `search_commands`)는 순수 조회용이라 이름/설명/사용법만
알려주고 실행 argv가 없다(엑셀 업로드는 텍스트 메타데이터일 뿐 실제 실행 가능한 인자 목록이
아님). "매뉴얼 db에서 커맨드 찾아서 그냥 실행"을 코드 그대로 구현하면, 카탈로그에 적힌 임의
텍스트를 검증 없이 셸 실행하는 것과 같아져서 위험하다(엑셀 대량 업로드 데이터라 사람이 한 줄씩
검토 안 했을 수 있음 — `rm`처럼 위험한 게 섞여 있어도 그대로 실행됨).

제안하는 안전한 방식: System MCP의 "커스텀 커맨드" 기능(이미 만들어져 있음 — argv_template +
파라미터를 관리자가 등록하고, `shared/custom_commands.py`가 위험한 기본 명령을 거부)과 동일한
매커니즘을 Command MCP에도 추가한다. 카탈로그에서 "이 커맨드 실행 가능하게 등록" 버튼으로
argv_template을 지정하면, 그 뒤로는 검증된 실행이 가능해진다(로그인 서버 고정, System MCP
커스텀 커맨드와 동일한 안전장치). 이렇게 갈지, 아니면 다른 방식을 원하는지 확인 부탁 — 안전
검증 없이 카탈로그 텍스트를 그대로 실행하는 방식은 권장하지 않음.

완료된 내역/원인 분석은 `docs/RUN-LOG.md` 참고.
