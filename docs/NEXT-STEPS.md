# 지금 할 일

## 지금 당장

ssh 키 등록 완료 확인됨(`ssh root@202.20.185.100 whoami` → `root`, 비밀번호 없이 성공). 남은 건:

### 1) 코드 최신화 — 설정 탭에 `scheduler_login_host`가 안 보이던 버그 수정됨
```bash
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
rsync -avz --delete --exclude '.env' --progress /home/yrc/AI-Infra-Assistant/ \
  yr9.choi@202.20.185.100:/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/
```
```bash
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml restart admin-console
```
(원인: 설정 그룹 분류 목록 어디에도 안 걸려서 "기타" 밑에 묻혀있었음 — "SSH 실행 (System/Command
MCP)"라는 별도 그룹으로 분리해서 이제 바로 보임)

### 2) 설정 탭 — 로그인 서버 반영 + 에이전트가 안 되묻게 지시문 추가

- 설정 탭 → **"SSH 실행 (System/Command MCP)"** 그룹 → `scheduler_login_host` = `login07` 저장.
- `agent_system_instruction`에 아래 문장 추가 후 저장 (재시작 필요 키 → 저장 후 "agent-server
  재시작" 버튼 클릭):
  ```
  시스템 점검 관련 질문(디스크/홈 스토리지 할당량, GPU 상태 등)에서 사용자가 서버를 명시하지
  않으면 로그인 서버 host='login07'로 간주하고 바로 해당 System MCP 툴을 호출하세요.
  서버를 되묻지 마세요.
  ```

### 3) 확인
open-webui에서 "홈 스토리지 할당량 알려줘"로 재질문 → 바로 `login07`(=202.20.185.100 경유)로
조회해서 답하는지 확인. admin_console → System MCP 탭 → 실행 로그에서 `disk_free` 호출이 찍히고
`status`가 `success`인지 확인.

---

**참고 — "무슨 일이 있어도 root 권한으로 커맨드를 실행하면 안 된다"는 요구사항은 이미 코드로
보장돼 있음**: `shared/ssh_exec.py`의 `run_ssh_as_user()`가 모든 System/Command MCP 툴의
유일한 실행 경로이고, 매번 원격에서 `su - <user_id> -c '<커맨드>'`로 감싸서 실행한다(우회 경로
없음). ssh 자체는 root로 접속하지만 실제 명령은 항상 호출자 user_id 권한으로 강등돼서 돈다.

완료된 내역/원인 분석은 `docs/RUN-LOG.md` 참고.
