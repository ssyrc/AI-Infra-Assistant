# 지금 할 일

**[WSL]** `/home/yrc/AI-Infra-Assistant`
**[서버]** 202.20.183.30 · `/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant`
**[웹]** 관리자 콘솔 `http://202.20.183.30:8501`

> 아직 서버에 반영되지 않은 것이 쌓여 있습니다(#128~#140). 1~4번을 한 번에 하시면 됩니다.
> 이번에 들어간 것: **보안 강화**(내부 호출 인증 · 남의 계정 조회 차단) ·
> **엑셀로 커맨드 일괄 등록**(인자 포함) · 인자 설명이 에이전트에 실제로 전달되도록 수정.

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

## 2. [서버] `.env` 확인 — **ssh 키를 꺼낼 필요 없습니다**

호스트에 `/root/.ssh/id_ed25519` 가 있으니 그걸 그대로 씁니다(복사하지 마세요).

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
ls -l /root/.ssh/id_ed25519          # 파일이어야 합니다(디렉토리면 멈추고 알려주세요)
cp -n .env.example .env
grep -q SSH_KEY_PATH .env || printf '\nSSH_KEY_PATH=/root/.ssh/id_ed25519\nSSH_PRIVDROP=runuser\n' >> .env
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

- `db-init`을 빼면 새 설정 키(`execution_user_scope_flags` 등)가 안 생깁니다.
- `restart admin-console`을 빼면 4번 버튼이 **`405 Method Not Allowed`** 로 실패합니다
  (화면은 rsync로 바로 새 코드가 되는데 백엔드는 재시작해야 바뀝니다).
- 마지막 줄이 `-rw------- 1 root root 399 ...` 처럼 **파일**이어야 합니다.

## 4. [웹] 콘솔 → 설정 탭 → 에이전트

**`지시문을 최신 기본값으로 되돌리기`** 버튼 클릭 → 이어서 나오는 `agent-server 재시작` 승인.
(이번에 지시문이 바뀌었습니다 — 이걸 안 누르면 남의 계정 질문에 계속 가이드 문서를 안내합니다.)

같은 화면에서 값 확인 · 입력:

| key | 값 |
|---|---|
| `execution_host` | `202.20.185.100` |
| `openwebui_public_url` | `http://202.20.183.30:8502` |
| `voc_intake_guide` | 실제 VOC 접수 경로 |
| **`agent_api_key`** | **Open WebUI 연결(Connections)에 넣은 API 키와 같은 값** |

`agent_api_key`를 넣어야 `/v1/*`에 인증이 걸립니다. 비워 두면 **같은 망의 누구나 헤더만 바꿔
남의 계정으로 커맨드를 실행할 수 있습니다.** 넣은 뒤 agent-server를 재시작하고 Open WebUI에서
질문이 되는지 확인하세요(키가 서로 다르면 모델 목록이 비고 401이 납니다).
키 확인: Open WebUI 관리자 패널 → 설정 → 연결(Connections) → `http://agent-server:8000/v1`.

## 5. [웹] 콘솔 → 커맨드 실행 탭 → 엑셀로 등록된 커맨드 정리

**`현재 등록분 내보내기`** 를 눌러 지금 등록된 커맨드를 엑셀로 받으세요. 거기서 고치고
그대로 다시 올리면 이름 기준으로 덮어써집니다. (빈 양식은 `엑셀 양식 받기`.)

지금 `myquota` / `s2_phd_list` / `s2_phd_info` 세 개가 전부 인자 타입 '문자열'로 되어 있는데,
`{option}` 은 **선택형**으로 바꾸는 게 맞습니다. 엑셀에서 이렇게 적으시면 됩니다.

| 이름 | 실행 커맨드 | 인자1 이름 | 인자1 타입 | 인자1 선택지 |
|---|---|---|---|---|
| s2_phd_list | `phd list {option}` | (비워도 됨) | 선택형 | `-l: 상세 정보를 길게 출력`<br>`-lf: 선택 가능한 필드 목록` |
| s2_phd_info | `phd info {option} {job_id}` | (비워도 됨) | 선택형 | `-j: JSON 형식으로 반환`<br>`-tl: 부가 정보까지 출력` |

- **인자 이름 칸은 비워도 됩니다** — 실행 커맨드의 `{option}` `{job_id}` 순서로 자동 연결됩니다.
- 선택지는 **한 칸 안에서 줄바꿈**(`Alt+Enter`)으로 하나씩, `값: 설명` 형태로 씁니다.
  콜론 뒤에 **공백**을 넣어야 값과 설명이 갈립니다.
- 콤마로 구분하지 마세요(설명에 콤마가 들어가면 깨집니다).
- 올린 뒤 **execution-mcp 재시작**(화면에 버튼이 뜹니다).

지금까지는 여기 적은 인자 설명이 **에이전트에게 전혀 전달되지 않았습니다**(#140에서 수정).
이제 전달되므로, 설명을 제대로 적으면 옵션 선택이 눈에 띄게 좋아질 겁니다.

## 6. [웹] Open WebUI에서 확인 — 각 항목 **소요 시간**을 적어 주세요

1. `S2 스케줄러 job list 확인해줘`
2. `내 홈 파일 리스트 보여줘`
3. `내 홈 스토리지 용량 얼마나 써?`
4. `cocoa.song 계정이 어떤 gpu job 을 수행중이야?` ← **이번에 고친 것**

4번은 이렇게 한 줄로만 나와야 합니다.

> 본인(yr9.choi) 자원만 조회할 수 있어 cocoa.song의 job은 확인할 수 없습니다.

`ops_assistant`라는 말이 나오거나, "가이드 위치: 슈퍼컴 Portal > …" 이 붙으면 **4번의
지시문 되돌리기를 안 한 것**입니다.

1~3번은 진행 줄을 **통째로** 보내주세요.

```
· `ls -lh` 실행하는 중
· 완료 (202.20.185.100 · yr9.choi · 0.4초) ⚠ 출력 132줄 중 58줄만
```

- 앞줄 = **실제로 실행된 커맨드**. 숨김 파일이 안 보이면 여기에 `-A`가 없는 것입니다.
- `(202.20.185.100 · yr9.choi)` = 로그인 서버에서 본인 계정으로 돌았다는 **증거**입니다
  (답변 본문의 "…기준입니다" 같은 문장은 근거가 아닙니다).
- `⚠ 출력 N줄 중 M줄만` 이 보이면 목록이 잘린 것입니다. 자주 보이면 설정 탭의
  `execution_result_max_chars`(기본 4000)를 올리세요.

## 7. [서버] 로그 3종 — 6번 뒤에 보내주세요

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
docker compose -f docker-compose.dev.yml logs execution-mcp | grep -E "상주 마스터|노출된 툴|스키마"
docker compose -f docker-compose.dev.yml logs --tail=100 execution-mcp | grep ssh_exec
docker compose -f docker-compose.dev.yml logs --tail=100 agent-server | grep 완료
```

각각 이렇게 나와야 정상입니다.

- `상주 마스터 준비 완료` — root 세션이 떠 있음
- `등록 3개 · run_command 1개 = 툴 4개 (스키마 …자 ≈ …토큰/요청)` — 인자 설명이 포함된 값
- `[ssh_exec] phd 320ms (연결 재사용 · runuser · ...)` — 접속 0, 로그인 셸 생략
- `[agent] chatcmpl-… 완료 8.1초 (준비 0.3초 · 첫 글자 3.2초 · 도구 1회 · 커맨드 실행 0.4초)`

`준비`가 크면 MCP 세션, `커맨드 실행`이 크면 실행 쪽, `전체 − 커맨드 실행`이 크면 LLM 턴 수가
문제입니다. 그 숫자로 다음 작업을 정합니다.

---

## 문제가 계속될 때만

| 증상 | 조치 |
|---|---|
| 멀쩡한 커맨드가 `다른 사용자의 자원을 조회하려는 것` 으로 거부됨 | 설정 탭 `execution_user_scope_flags` 에서 걸리는 옵션(예: `-u`)을 빼세요. `sort -u` 처럼 계정과 무관한 `-u`가 있습니다 |
| 엑셀 업로드 후 일부가 `건너뜀` | 사유가 화면에 나옵니다. 대부분 자리표시자와 인자 열 개수가 안 맞는 경우 — 인자 이름 칸을 **비우면** 자동으로 맞춰집니다 |
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
(`ps -a`로 확인).
