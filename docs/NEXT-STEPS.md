# 지금 할 일

## 지금 당장

### 1) Open WebUI 데이터 초기화 (이메일 DB 꼬임) — postgres/플랫폼 데이터는 안 건드림

```bash
docker compose -f docker-compose.dev.yml stop open-webui
docker compose -f docker-compose.dev.yml rm -f open-webui
docker volume ls | grep open_webui_dev_data   # 정확한 볼륨 이름 확인(프로젝트명 접두사 붙음)
docker volume rm <위에서 확인한 볼륨 이름>
docker compose -f docker-compose.dev.yml up -d open-webui
```
⚠️ 이 볼륨만 지우는 것 — `docker compose down -v`는 절대 쓰지 말 것(postgres 등 전체 데이터가
같이 날아감). 초기화 후 `:8502` 접속하면 처음 상태로 돌아가서 회원가입부터 다시 해야 함(첫 계정이
자동 admin).

### 2) 초기화 후 — 반드시 "연결(Connections)"에 다시 등록 (계정 API 키와는 다른 것!)

**중요한 오해 정리**: Open WebUI 개인 계정의 "API 키"(설정 → 계정 → API 키)를 admin_console의
"Open WebUI 연동" 설정에 넣는 것과, 모델이 보이고 채팅이 되게 하는 것은 **완전히 다른 두 가지
기능**임:

| 무엇을 | 어디에 등록 | 무슨 효과 |
|---|---|---|
| Open WebUI 계정 API 키 | admin_console 설정 탭 → "Open WebUI 연동" → `openwebui_admin_api_key` | "Open WebUI 기본 모델 동기화" 버튼이 동작하게 함(부가 기능) |
| `http://agent-server:8000/v1` + 아무 키 | **Open WebUI 자체** 관리자 패널 → 설정 → **연결(Connections)** → OpenAI API | 모델이 뜨고 채팅이 되게 함(핵심 기능, 이게 없으면 아무것도 안 됨) |

1번으로 초기화한 뒤:
1. `:8502` 접속 → 회원가입으로 admin 계정 생성
2. 관리자 패널(Admin Panel) → 설정(Settings) → **연결(Connections)** (계정/API 키 화면 아님)
3. OpenAI API 섹션에 URL `http://agent-server:8000/v1`, 키에 아무 값(`not-needed`) 입력 후 저장
4. 새로고침해서 "AI Infra Assistant" 모델이 보이고 채팅되는지 확인
5. (선택) 일반 사용자 계정도 만들어서 같은 모델이 보이는지 확인 — 연결은 관리자 패널에 등록하면
   전체 사용자에게 공통 적용됨

완료된 내역/원인 분석은 `docs/RUN-LOG.md` 참고.
