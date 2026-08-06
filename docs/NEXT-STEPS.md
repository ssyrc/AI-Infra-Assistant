# 지금 할 일

**[서버]** 202.20.183.30 · `/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant`
**[WSL]** `/home/yrc/AI-Infra-Assistant`
**[웹]** 관리자 콘솔 `http://202.20.183.30:8501` · Open WebUI `http://202.20.183.30:8502`

> ⛔ 며칠 정상 동작을 확인할 때까지 `docker volume prune` · `system prune` · `down -v` 금지.
> 남은 익명 볼륨 2개가 마지막 보험입니다(맨 아래에서 정리).

이번 세 가지 증상은 **전부 답변 품질** 문제입니다(실행 자체는 정상).
`ls -la` 16행만 표시 · 홈 경로를 지어냄 · job 목록을 표로 안 만듦.

---

## 1. [서버] 지시문이 실제로 반영됐는지 **먼저 확인**

`내 홈 디렉토리는 어디야?` → `/home/yr9.choi`(틀림)는 **옛 지시문이 살아 있을 때 나오는
증상**입니다. 옛 지시문은 확인용 커맨드를 돌리지 말라고 적극적으로 막고 있었습니다.
추측하기 전에 이것부터 가릅니다.

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
docker exec ai-infra-assistant-postgres-1 psql -U agent -d platform_config -tAc \
  "select case when value like '%행을 임의로 줄이지 않습니다%'
            then '최신 지시문 OK'
            else '옛 지시문 - 콘솔에서 되돌리기 버튼을 눌러야 합니다' end
     from platform_settings where key='agent_system_instruction';"
```

**`옛 지시문`이 나오면 그 상태의 답변 품질은 판단 근거가 되지 않습니다.** 2번을 먼저 하세요.

## 2. [서버] 코드 반영 + 지시문 되돌리기

`ls -la` 16행만 나온 것은 **모델이 임의로 줄인 것**입니다. 16행이면 약 1,200자라
`execution_result_max_chars`(4000)에 한참 못 미칩니다 — 우리가 자른 게 아닙니다.
`phd list` 결과를 표로 안 만든 것도 같은 뿌리입니다. 지시문을 이렇게 고쳤습니다.

> **행을 임의로 줄이지 않습니다. 이게 가장 자주 나는 사고입니다.**
> 도구가 돌려준 행이 30줄이면 30줄을 전부 답에 넣습니다. 도구 결과에
> `전체 N줄 중 M줄만 보입니다`가 붙어 있을 때만 잘린 것이고, 그때는 몇 줄 중 몇 줄인지
> 답에 밝힙니다. 그 안내가 없으면 받은 것이 전부이므로 전부 보여줍니다.

```bash
# [서버]
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
bash scripts/backup-db.sh

# [WSL]
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
bash /home/yrc/AI-Infra-Assistant/scripts/deploy-rsync.sh

# [서버]
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
docker compose -f docker-compose.dev.yml restart agent-server
```

그다음 **콘솔 설정 탭 → `지시문을 최신 기본값으로 되돌리기` → agent-server 재시작.**
1번 확인 커맨드를 다시 돌려 `최신 지시문 OK`가 나오는지 보세요.

## 3. [웹] 세 질문 다시 확인

| 질문 | 기대 |
|---|---|
| `내 홈 파일 리스트 보여줘` | 도구가 돌려준 **모든 행**. 잘렸으면 "N줄 중 M줄" 명시 |
| `내 홈 디렉토리는 어디야?` | **실행해서 나온 값** — `/home/gpu1/yr9.choi` |
| `내 s2 gpu job 리스트 보여줘` | 표로 정리 |

여전히 이상하면 **진행 줄을 통째로** 보내주세요.

```
· `ls -la` 실행하는 중
· 완료 (202.20.185.100 · yr9.choi · 0.4초) ⚠ 출력 132줄 중 58줄만
```

- `⚠`가 **없는데** 답변에서 행이 빠졌다 → 모델이 자른 것(지시문 문제)
- `⚠`가 **있는데** 답변에 안 밝혔다 → 지시문 반영 안 됨
- `⚠`가 자주 보인다 → 설정 탭 `execution_result_max_chars`(기본 4000)를 올리세요

## 4. [웹] 커맨드 인자를 **선택형**으로 — `-a` 지어내기를 막습니다

`phd list -a`가 나간 것은 **모델이 `-a`를 지어냈기** 때문입니다. 등록된 인자가 아직
'문자열'이라 무엇이든 넣을 수 있습니다. **선택형으로 바꾸면 스키마에 값이 박혀 지어낼 수
없게 됩니다.**

콘솔 → 커맨드 실행 탭 → **`현재 등록분 내보내기`** → 엑셀에서 수정 → 업로드 →
**`execution-mcp 재시작`**.

| 이름 | 실행 커맨드 | 인자1 타입 | 인자1 선택지 |
|---|---|---|---|
| s2_phd_list | `phd list {option}` | 선택형 | `-l: 상세 정보를 길게 출력`<br>`-lf: 선택 가능한 필드 목록` |
| s2_phd_info_job_id | `phd info {option} {job_id}` | 선택형 | `-j: JSON 형식으로 반환`<br>`-tl: 부가 정보까지 출력` |

- **인자 이름 칸은 비워도 됩니다** — `{option}` `{job_id}` 순서로 자동 연결됩니다.
- 선택지는 한 칸 안에서 **줄바꿈(Alt+Enter)** 으로 하나씩, `값: 설명` 형태.
  **콜론 뒤에 공백**이 있어야 값과 설명이 갈립니다. 콤마로 나누지 마세요.
- 자주 쓰는 형태가 있으면 **기본값** 칸에 넣으세요(비워 두면 옵션 없이 실행).

## 5. [서버] Service Hub 연동

`docs/SERVICE-HUB.md` 를 Service Hub 팀에 전달하세요. 먼저 이 curl이 서버에서 되는지
확인하고 결과를 보내주세요.

```bash
KEY=<agent_api_key>
curl -s -X POST http://202.20.183.30:8500/v1/voc/query \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"voc_info":{"voc_id":"TEST-1","voc_title":"테스트",
       "requester":{"user_id":"yr9.choi"},
       "voc_content":{"text":"내 GPU job 목록 알려줘"}},
       "output_option":"markdown"}' | python3 -m json.tool
```

유사 VOC(`similar_voc`)를 붙이려면 Service Hub MCP 주소를 받아 콘솔 설정 탭
`service_hub_mcp_url`에 넣으세요. 비워 두면 그 부분만 생략되고 나머지는 정상 동작합니다.

---

## 며칠 뒤 (정상 동작 확인 후)

남은 익명 볼륨을 정리합니다. **그 전에는 두세요.**

```bash
docker volume rm 553dd7066a559e45d37bb0d7d7d4b47fadeff60309477e7b9a8ebe0d6a769448
docker volume rm 1dc7527fd826d5a2afc08bd1b44e945219c2fd10da65c2747f49c2d367ab9198
```

## 문제가 생기면

| 증상 | 조치 |
|---|---|
| 답변이 행을 조용히 줄임 | 1번으로 지시문 버전 확인. `옛 지시문`이면 되돌리기 |
| 홈 경로를 지어냄 | 같음. 최신 지시문이면 실행해서 답합니다 |
| 모델이 없는 옵션을 지어냄 | 4번에서 그 인자를 **선택형**으로 바꾸세요 |
| 모델 목록이 비고 질문이 401 | 콘솔 `agent_api_key`와 Open WebUI 연결 키가 다릅니다 |
| Service Hub curl 이 401 | 같은 키를 `Authorization: Bearer` 로 보내야 합니다 |
| 401인데 방금 키를 바꿨다 | 설정 캐시 5초입니다. 잠시 뒤 다시 |
| 멀쩡한 커맨드가 `다른 사용자의 자원…`으로 거부 | 설정 탭 `execution_user_scope_flags`에서 그 옵션을 빼세요 |
| 엑셀 업로드 후 일부 `건너뜀` | 사유가 화면에 나옵니다. 대개 인자 이름 불일치 — **이름 칸을 비우면** 자동으로 맞춰집니다 |
| `Expecting value: ...` 반복 | 설정 탭 `llm_streaming` → `false` → agent-server 재시작 |
| 커맨드가 `command not found` | 로그에 `로그인 셸이 필요합니다`가 있으면 자동 복구된 것. 계속되면 `.env`에 `SSH_PRIVDROP=su-login` |
| 되돌려야 할 때 | `DROP_EXISTING=yes bash scripts/restore-db.sh ../pg-backup-<날짜>.sql` |
