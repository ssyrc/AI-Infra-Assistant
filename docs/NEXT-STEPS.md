# 지금 할 일

## 지금 당장

### 1) 정정: `login07`은 202.20.185.10이 아니라 게이트 서버(202.20.185.100)로 접속

202.20.185.10은 직접 못 붙고, **202.20.185.100이 로그인 서버로 자동 라우팅해주는 게이트
서버**라고 확인해줌(202.20.185.100 자체의 `/etc/hosts`에 내부 서버들이 등록돼 있고, 거기로
ssh하면 알아서 연결됨). 배포 호스트(202.20.183.30)의 `/etc/hosts`에 등록한 줄을 정정:
```bash
sudo sed -i '/login07/d' /etc/hosts   # 아까 잘못 넣은 202.20.185.10 줄 제거
echo '202.20.185.100  login07' | sudo tee -a /etc/hosts
```
(우리 쪽 코드는 그대로 — `resolve_host()`가 `login07`을 202.20.185.100으로 풀어서 그 한 곳에만
ssh하면 되고, 실제 목표 서버로의 라우팅은 게이트 서버 쪽에서 알아서 처리됨)

### 2) ssh 키가 비밀번호를 요구하는 문제 — 아직 202.20.185.100에 공개키 등록 전이라 정상

```
[root@24-gp19 ~]# ssh -i .../secrets/id_ed25519 root@202.20.185.100 whoami
root@202.20.185.100's password:
```
개인키를 로컬에 복사해둔 것만으로는 안 되고, **그 공개키를 202.20.185.100의
`/root/.ssh/authorized_keys`에 등록**해야 비밀번호 없이 접속됨:
```bash
ssh-copy-id -i /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/secrets/id_ed25519.pub root@202.20.185.100
```
(root 비밀번호를 한 번 물어봄 — 입력하면 등록됨). 확인:
```bash
ssh -i /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/secrets/id_ed25519 root@202.20.185.100 whoami
```
비밀번호 없이 `root`가 바로 출력되면 준비 끝.

### 3) 컨테이너 재생성 (아직 안 했으면 — ssh 볼륨/환경변수는 이미 코드에 반영돼 있음)
```bash
docker compose -f docker-compose.dev.yml up -d
```

### 4) 설정 탭 — 로그인 서버 이름 반영 + 에이전트가 안 되묻게 지시문 추가

- `scheduler_login_host` = `login07` 저장 (Command MCP의 job 조회용, hot_reload라 재시작 불필요).
- `agent_system_instruction`에 아래 문장 추가 후 저장(이 키는 재시작 필요 → 저장 후 "agent-server
  재시작" 버튼 클릭):
  ```
  시스템 점검 관련 질문(디스크/홈 스토리지 할당량, GPU 상태 등)에서 사용자가 서버를 명시하지
  않으면 로그인 서버 host='login07'로 간주하고 바로 해당 System MCP 툴을 호출하세요.
  서버를 되묻지 마세요.
  ```

### 5) 확인
```bash
docker compose -f docker-compose.dev.yml logs system-mcp --tail 30
```
open-webui에서 "홈 스토리지 할당량 알려줘"로 재질문 → 바로 `login07`로 조회해서 답하는지 확인.
admin_console → System MCP 탭 → 실행 로그에서 `disk_free` 호출이 이제 찍히는지, `status`가
`success`인지도 확인.

완료된 내역/원인 분석은 `docs/RUN-LOG.md` 참고.
