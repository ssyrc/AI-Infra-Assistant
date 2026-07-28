# 지금 할 일

## 지금 당장

### 1) 이메일 변경 커맨드 수정 — 이미지에 `sqlite3` CLI가 없음

`sqlite3: executable file not found` — open-webui 이미지엔 sqlite3 바이너리가 없다. 대신 이미
들어있는 python3의 표준 라이브러리로 같은 작업을 한다:
```bash
# 1) 현재 계정 확인
docker compose -f docker-compose.dev.yml exec open-webui python3 -c \
  "import sqlite3; c=sqlite3.connect('/app/backend/data/webui.db'); \
   print(c.execute('SELECT id, name, email, role FROM user').fetchall())"

# 2) 이메일 변경
docker compose -f docker-compose.dev.yml exec open-webui python3 -c \
  "import sqlite3; c=sqlite3.connect('/app/backend/data/webui.db'); \
   c.execute(\"UPDATE user SET email=? WHERE email=?\", ('새이메일@example.com', '기존이메일@example.com')); \
   c.commit(); print('done')"
```
바꾼 뒤 로그아웃하고 새 이메일로 다시 로그인.

### 2) "모델을 admin 계정에서만 보이고 일반 사용자는 안 보임 / admin도 채팅 시 Model not found"

**십중팔구 API 키를 Open WebUI "관리자 패널"이 아니라 개인 계정(우측 상단 프로필 → 설정 →
연결)에 등록했을 가능성이 큼** — 그건 그 계정에서만 보이는 "개인 연결(Direct Connections)"이라
다른 사용자에게 안 뜨는 게 정상이다. 요청하신 대로 "admin이 키 하나만 등록하면 전체 사용자에게
default로 뜨게" 하려면 반드시 **관리자 전용 설정**에 등록해야 한다:

1. 좌측 하단(또는 우측 상단) 프로필 → **관리자 패널(Admin Panel)** 클릭 (개인 "설정"이 아님 —
   메뉴 이름 구분 필요)
2. 관리자 패널 → **설정(Settings) → 연결(Connections)**
3. "OpenAI API" 섹션에 `http://agent-server:8000/v1` + 아무 API 키(예: `not-needed`) 등록
   (개인 계정 설정이 아니라 여기여야 전체 사용자에게 공통 적용됨)
4. 저장 후 admin 계정에서 로그아웃 → 아무 계정(admin/일반 사용자 모두)으로 재로그인해서 모델이
   보이는지, 채팅이 되는지 확인

혹시 이미 "관리자 패널 → 연결"에 등록했는데도 이 증상이면(개인 설정이 아니었다면), admin으로
채팅 보낼 때 뜨는 "Model not found" 에러 화면의 **브라우저 개발자 도구 콘솔(F12) 로그 전체**를
같이 보내달라 — 실제 요청에 어떤 model id가 찍히는지 봐야 정확한 원인을 알 수 있음.

완료된 내역/원인 분석은 `docs/RUN-LOG.md` 참고.
