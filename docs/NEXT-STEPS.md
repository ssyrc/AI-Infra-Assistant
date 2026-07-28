# 지금 할 일

## 지금 당장

### 1) "AI Infra Assistant"가 모델 목록에서 안 보임 — 원인 좁혀짐

`curl http://202.20.183.30:8500/v1/models`는 정상(`"AI Infra Assistant"` 나옴). open-webui
로그에는 `host.docker.internal:11434`(Ollama 기본 포트) 연결 실패만 있고 — 이건 무시해도 됨,
Open WebUI가 기본으로 Ollama도 찾아보려다 없어서 나는 에러라 우리 설정과 무관함. **중요한 건
로그에 agent-server(OpenAI 연결) 관련 시도 자체가 안 보인다는 것** — WEBUI_AUTH를 켜고 볼륨을
새로 만들면서 Open WebUI의 "연결(Connections)" 설정이 비어 있을 가능성이 높음.

확인:
```bash
docker compose -f docker-compose.dev.yml exec open-webui env | grep OPENAI
```
`OPENAI_API_BASE_URL=http://agent-server:8000/v1`이 나오는지 확인(안 나오면 컨테이너 재생성
필요 — `docker compose -f docker-compose.dev.yml up -d --force-recreate open-webui`).

env는 있는데도 안 뜨면 UI에서 연결을 확인/추가:
1. `:8502` → 프로필 → 관리자 패널 → 설정 → **연결(Connections)**
2. "OpenAI API" 섹션에 `http://agent-server:8000/v1` 항목이 있는지 확인
3. 없으면 **+ 추가**: URL에 `http://agent-server:8000/v1`, API 키에 아무 값(예: `not-needed`)
   입력 후 저장
4. 저장 후 관리자 패널 → 설정 → **모델(Models)**에서 "AI Infra Assistant"가 뜨는지, 꺼져
   있으면 켜기

### 2) Open WebUI 관리자 계정 이메일 직접 변경 (DB 수정)

관리자 패널에서 본인 이메일은 못 바꾸므로(Open WebUI 자체 제약), DB를 직접 고친다:
```bash
# 1) 현재 이메일 확인
docker compose -f docker-compose.dev.yml exec open-webui \
  sqlite3 /app/backend/data/webui.db "SELECT id, name, email, role FROM user;"

# 2) 이메일 변경 (기존 이메일 자리에 지금 쓰는 값을, 새 이메일 자리에 바꿀 값을 넣는다)
docker compose -f docker-compose.dev.yml exec open-webui \
  sqlite3 /app/backend/data/webui.db \
  "UPDATE user SET email='새이메일@example.com' WHERE email='기존이메일@example.com';"
```
바꾼 뒤에는 로그아웃하고 **새 이메일**로 다시 로그인해야 함(비밀번호는 그대로).

완료된 내역/원인 분석은 `docs/RUN-LOG.md` 참고.
