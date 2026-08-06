# 지금 할 일

**[WSL]** `/home/yrc/AI-Infra-Assistant`
**[서버]** 202.20.183.30 · `/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant`
**[웹]** 관리자 콘솔 `http://202.20.183.30:8501`

> 아직 아무것도 반영되지 않았습니다. 지난 수정(첫 접속 17초, root 세션 상주, ADK 툴 호출
> 오류 우회, 로그인 셸 생략)이 전부 `main`에만 있습니다.
>
> **2번을 건너뛰지 마세요.** ssh 키가 지워진 상태라, 그냥 재생성하면 실행이 전부 막힙니다.

---

## 1. [WSL] 코드 받아서 서버로

```bash
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
rsync -avz --delete --progress \
  --exclude '.env' --exclude 'secrets/' \
  /home/yrc/AI-Infra-Assistant/ \
  yr9.choi@202.20.185.100:/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/
```

## 2. [서버] `.env` 만들기 — **키를 꺼낼 필요 없습니다**

호스트에 `/root/.ssh/id_ed25519` 가 있으니 그걸 그대로 쓰면 됩니다(복사하지 마세요).
compose 기본값도 이 경로로 바꿔 뒀습니다.

`.env`가 없던 이유는 **rsync `--delete`가 지웠기 때문**입니다(`.env`가 저장소에 없는 파일이라).
1번의 rsync는 이제 `.env`와 `secrets/`를 제외하므로 다시 지워지지 않습니다.

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
ls -l /root/.ssh/id_ed25519          # 파일이어야 합니다(디렉토리면 멈추고 알려주세요)
cp -n .env.example .env
printf '\nSSH_KEY_PATH=/root/.ssh/id_ed25519\nSSH_PRIVDROP=runuser\n' >> .env
```

## 3. [서버] 재생성

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
docker compose -f docker-compose.dev.yml run --rm db-init
docker compose -f docker-compose.dev.yml up -d --no-build --remove-orphans
docker compose -f docker-compose.dev.yml restart admin-console
docker compose -f docker-compose.dev.yml ps
docker compose -f docker-compose.dev.yml exec -T execution-mcp ls -l /root/.ssh/id_ed25519
```

`restart admin-console`을 빼면 4번의 버튼이 **`405 Method Not Allowed`** 로 실패합니다.
화면(정적 파일)은 rsync로 바로 새 코드가 되는데 백엔드는 재시작해야 바뀌기 때문입니다.

마지막 줄이 `-rw------- 1 root root 399 ...` 처럼 **파일**이어야 합니다(디렉토리면 키가 없는 것).

로그인 셸 생략(`SSH_PRIVDROP=runuser`)은 확인 없이 켜도 됩니다 — 안 되는 커맨드는 시스템이
알아서 로그인 셸로 되돌려 실행합니다.

## 4. [웹] 콘솔 → 설정 탭 → 에이전트

**`지시문을 최신 기본값으로 되돌리기`** 버튼 클릭 → 이어서 나오는 `agent-server 재시작` 승인.

> 이제 지시문을 복사·붙여넣지 않습니다. 이번 지시문에서 `phd info` 라는 이름을 없앴는데,
> 그게 "지어내지 마세요"라는 금지 예시로 들어 있어서 모델이 그대로 실행하고 있었습니다.

같은 화면에서 값 확인:

| key | 값 |
|---|---|
| `execution_host` | `202.20.185.100` |
| `openwebui_public_url` | `http://202.20.183.30:8502` |
| `voc_intake_guide` | 실제 VOC 접수 경로 |

## 5. [웹] 콘솔 → 커맨드 실행 탭

`phd info -u {user_id}` 처럼 **동작하지 않는 커맨드가 등록돼 있으면 지우거나 고칩니다.**
등록된 커맨드는 그대로 에이전트 툴이 되므로, 여기 있으면 계속 호출됩니다.

## 6. [웹] Open WebUI에서 확인 — 각 항목 **소요 시간**을 적어 주세요

1. `S2 스케줄러 job list 확인해줘`
2. `내 홈 파일 리스트 보여줘`
3. `내 홈 스토리지 용량 얼마나 써?`

답변 위 진행 줄에 `· 완료 (202.20.185.100 · yr9.choi · 2.3초)` 처럼 초가 찍힙니다.
**어떤 커맨드를 실행했는지**도 함께 알려 주세요.

## 7. [서버] 로그 3종 — 6번 뒤에 보내주세요

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
docker compose -f docker-compose.dev.yml logs execution-mcp | grep -E "상주 마스터|노출된 툴"
docker compose -f docker-compose.dev.yml logs --tail=100 execution-mcp | grep ssh_exec
docker compose -f docker-compose.dev.yml logs --tail=100 agent-server | grep 완료
```

각각 이렇게 나와야 정상입니다.

- `상주 마스터 준비 완료` — root 세션이 떠 있음
- `[ssh_exec] phd 320ms (연결 재사용 · runuser · ...)` — 접속 0, 로그인 셸 생략
- `[agent] chatcmpl-… 완료 8.1초 (준비 0.3초 · 첫 글자 3.2초 · 도구 1회 · 커맨드 실행 0.4초)`

`준비`가 크면 MCP 세션, `커맨드 실행`이 크면 실행 쪽, `전체 − 커맨드 실행`이 크면 LLM 턴 수가
문제입니다. 그 숫자로 다음 작업을 정합니다.

---

## 문제가 계속될 때만

| 증상 | 조치 |
|---|---|
| `Expecting value: ...` 오류 반복 | 설정 탭 `llm_streaming` → `false` → agent-server 재시작 |
| 커맨드가 `command not found` | 로그에 `'xxx'은 로그인 셸이 필요합니다`가 있으면 자동 복구된 것. 계속 실패하면 `.env`의 `SSH_PRIVDROP=su-login` |
| 첫 커맨드만 계속 느림 | `curl -s http://localhost:8504/warm` 결과를 보내주세요 |
| 강등 방식별 시간이 궁금할 때 | `bash scripts/bench-exec.sh yr9.choi "phd list"` |
| `docker compose exec` 가 아무것도 안 뱉음 | 아래 진단을 돌려 결과를 보내주세요 |

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
docker compose -f docker-compose.dev.yml ps          # execution-mcp 가 Up 인가
docker compose -f docker-compose.dev.yml ps -a       # 죽은 컨테이너는 ps에 안 나온다
docker compose -f docker-compose.dev.yml logs --tail=30 execution-mcp
```

`exec`는 **컨테이너가 Up일 때만** 됩니다. 죽어 있으면 `ps`에 안 보이고 조용히 실패합니다
(`ps -a`로 확인). 출력이 비었던 것도 대부분 이 경우입니다.
