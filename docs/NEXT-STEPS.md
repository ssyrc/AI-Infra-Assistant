# 지금 할 일

## 지금 당장

### 1) 원인 확정: dev에는 ssh 실행 인프라 자체가 없었음 (방금 배선함)

`disk_free` 실행 로그가 아예 없는 이유: `docker-compose.dev.yml`의 command-mcp/system-mcp에는
`HOSTS_FILE`/`SSH_KEY` 같은 ssh 설정과 `/etc/hosts`·개인키 마운트가 **아예 빠져 있었다**
(prod용 `docker-compose.yml`에만 있었음). LLM이 host를 모르니 툴 호출 자체를 안 해서 로그도
안 남은 것. prod와 같은 방식으로 dev compose에도 배선했다 — 이제 인프라만 채우면 된다.

### 2) 배포 호스트(202.20.183.30)에 로그인 서버 등록
```bash
echo '202.20.185.10  login07' | sudo tee -a /etc/hosts
```
(docker-compose가 이 파일을 컨테이너에 읽기전용으로 그대로 마운트한다 — 컨테이너 안이 아니라
**호스트**의 `/etc/hosts`에 추가해야 함)

### 3) ssh 개인키 준비

`login07`(202.20.185.10)에 root로 접속 가능한 키가 필요하다. 이미 있으면 3-a로, 없으면 3-b로:

**3-a) 이미 root ssh 키가 있으면** — 그 개인키를 리포 루트 기준 `./secrets/id_ed25519`로 복사
(또는 `.env`의 `SSH_KEY_PATH`를 실제 경로로 지정):
```bash
mkdir -p /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/secrets
cp <기존 개인키 경로> /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/secrets/id_ed25519
chmod 600 /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/secrets/id_ed25519
```

**3-b) 새로 만들어야 하면**:
```bash
ssh-keygen -t ed25519 -f /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/secrets/id_ed25519 -N ""
ssh-copy-id -i /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/secrets/id_ed25519.pub root@202.20.185.10
```
(`ssh-copy-id`는 login07에 root로 최소 한 번 비밀번호 접속이 가능해야 동작함 — 안 되면 관리자에게
공개키(`.pub` 파일 내용)를 전달해서 `login07`의 `/root/.ssh/authorized_keys`에 등록 요청)

확인:
```bash
ssh -i /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/secrets/id_ed25519 root@202.20.185.10 whoami
```
`root`가 출력되면 준비 끝.

### 4) 코드 반영 + 컨테이너 재생성 (볼륨/환경변수 바뀌어서 재시작만으론 안 됨)
```bash
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
rsync -avz --delete --exclude '.env' --progress /home/yrc/AI-Infra-Assistant/ \
  yr9.choi@202.20.185.100:/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/
```
```bash
docker compose -f docker-compose.dev.yml up -d
```

### 5) 설정 탭 — 로그인 서버 이름 반영 + 에이전트가 안 되묻게 지시문 추가

- `scheduler_login_host` = `login07` 로 저장 (Command MCP의 job 조회용, hot_reload라 재시작 불필요).
- `agent_system_instruction`에 아래 문장 추가 후 저장(이 키는 재시작 필요 → 저장 후 "agent-server
  재시작" 버튼 클릭):
  ```
  시스템 점검 관련 질문(디스크/홈 스토리지 할당량, GPU 상태 등)에서 사용자가 서버를 명시하지
  않으면 로그인 서버 host='login07'로 간주하고 바로 해당 System MCP 툴을 호출하세요.
  서버를 되묻지 마세요.
  ```

### 6) 확인
```bash
docker compose -f docker-compose.dev.yml logs system-mcp --tail 30
```
open-webui에서 "홈 스토리지 할당량 알려줘"로 재질문 → 바로 `login07`로 조회해서 답하는지 확인.
안 되면 admin_console → System MCP 탭 → 실행 로그에서 `disk_free` 호출이 이제 찍히는지, 찍힌다면
`status`가 뭔지 확인해서 알려줘.

완료된 내역/원인 분석은 `docs/RUN-LOG.md` 참고.
