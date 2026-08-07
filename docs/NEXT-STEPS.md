# 지금 할 일

**[서버]** 202.20.183.30 · `/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant`
**[WSL]** `/home/yrc/AI-Infra-Assistant`
**[웹]** 관리자 콘솔 `http://202.20.183.30:8501` · Open WebUI `http://202.20.183.30:8502`

> ⛔ 며칠 정상 동작을 확인할 때까지 `docker volume prune` · `system prune` · `down -v` 금지.
> 남은 익명 볼륨 2개가 마지막 보험입니다(맨 아래에서 정리).

**이번 핵심**: 실행 결과 **원문을 모델 답변 뒤에 그대로** 붙입니다(#150). 모델이 행을 줄여도
사용자는 전체를 봅니다 — LLM을 거치지 않으니 지시문 준수율과 무관합니다.

```
[모델 답변 · 표/요약]

---

**실행 결과 원문**

`ls -la` — 202.20.185.100 · yr9.choi · 132줄

```text
(132줄 전부)
```
```

---

## 1. [서버] 코드 반영 — **`admin-console`을 반드시 함께 재시작**

```bash
# [서버]
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
bash scripts/backup-db.sh

# [WSL]
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
bash /home/yrc/AI-Infra-Assistant/scripts/deploy-rsync.sh

# [서버]  db-init 을 먼저 — 새 설정 키(execution_raw_output 등)가 생깁니다
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
docker compose -f docker-compose.dev.yml run --rm db-init
docker compose -f docker-compose.dev.yml restart admin-console agent-server
```

`db-init`을 빼면 원문 블록 설정이 없어 **기본값(켜짐)으로만** 동작하고 콘솔에서 조절할 수
없습니다. `admin-console` 재시작도 **필수**입니다. 지난번 되돌리기 버튼이 옛 텍스트를 저장한 것은
모듈 캐시 때문이었고(#147), 재시작해야 그 캐시가 비워집니다.

## 2. [서버] 지시문이 최신인지 확인 — **매직 문자열 대신 파일과 통째로 비교**

> ✅ **이미 최신이었습니다.** 지난번 `아직 옛것 (12751자)`는 **제 확인 쿼리가 틀린 것**입니다.
> 12,751자가 바로 지금 코드의 지시문 길이입니다. #149에서 `어디야?` 나열을 지웠는데
> 확인 문자열은 #148 것을 그대로 둬서, 멀쩡한 상태를 "옛것"이라고 보고했습니다.

이제 문구를 찾지 않고 **코드 파일과 DB를 직접 비교**합니다. 지시문을 아무리 고쳐도
이 확인은 안 고쳐도 됩니다.

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
bash scripts/check-instruction.sh
```

```
코드 : 12751자 (sha fc0dbff1249c2143)
DB   : 12751자

✅ 최신입니다. DB의 지시문이 지금 코드와 같습니다.
```

`❌ 다릅니다`가 나오면 콘솔 설정 탭 → **`지시문을 최신 기본값으로 되돌리기`** →
agent-server 재시작. 그래도 그대로면 `admin-console`을 재시작하세요(모듈 캐시, #147).

## 3. [웹] 세 질문 다시 확인

| 질문 | 기대 |
|---|---|
| `내 홈 파일 리스트 보여줘` | 답변 뒤에 **원문 블록**이 붙고 거기 전체 행이 보임 |
| `내 홈 디렉토리는 어디야?` | **실행해서 나온 값** — `/home/gpu1/yr9.choi` |
| `내 s2 gpu job 리스트 보여줘` | 모델 정리 + 원문 블록 |

원문 블록이 안 보이면 설정 탭 `execution_raw_output`이 `true`인지 확인하세요.

### 원문 관련 설정 (설정 탭 → Execution MCP)

| key | 기본 | 뜻 |
|---|---|---|
| `execution_raw_output` | `true` | 원문 블록 on/off |
| `execution_raw_output_min_lines` | `2` | 이 줄 수 이상일 때만 붙임 |
| `execution_raw_output_max_chars` | `20000` | 원문 블록 자체 상한(에이전트에 넘기는 4000과 별개) |
| `execution_raw_output_summary` | `false` | 원문 뒤 요약. **켜면 LLM을 한 번 더 불러 몇 초 늦어집니다** |

요약까지 원하시면 `execution_raw_output_summary`를 `true`로 바꾸세요. 지연이 늘어서
기본은 꺼 뒀습니다 — 켜 보시고 체감 속도를 알려주세요.

## 4. [웹] 커맨드 인자를 **선택형**으로 — 엑셀에 드롭다운을 넣었습니다

`phd list -a`의 `-a`는 **모델이 지어낸 옵션**입니다. 인자가 '문자열'이라 뭐든 들어갑니다.
**선택형으로 바꾸면 스키마에 값이 박혀 지어낼 수 없습니다.**

콘솔 → 커맨드 실행 탭 → **`현재 등록분 내보내기`** → 엑셀에서 수정 → 업로드 →
**`execution-mcp 재시작`**.

이번 양식부터 **타입 · 필수 · 활성 · 실행 위치**는 셀을 클릭하면 드롭다운이 나옵니다.
직접 타이핑하면 거부됩니다(오타로 다른 뜻이 되는 것을 막습니다).

| 열 | 고를 수 있는 값 |
|---|---|
| 인자N 타입 | `문자열` · `정수` · `선택형` |
| 인자N 필수 | `Y` · `N` |
| 활성 | `Y` · `N` (비우면 Y) |
| 실행 위치 | `로그인 서버` · `대상 서버` (비우면 로그인 서버) |
| 필요 역할 | `admin` · `user` 제안 + 직접 입력 가능 (비우면 **누구나**) |

채울 내용:

| 이름 | 실행 커맨드 | 인자1 타입 | 인자1 선택지 |
|---|---|---|---|
| s2_phd_list | `phd list {option}` | 선택형 | `-l: 상세 정보를 길게 출력`<br>`-lf: 선택 가능한 필드 목록` |
| s2_phd_info_job_id | `phd info {option} {job_id}` | 선택형 | `-j: JSON 형식으로 반환`<br>`-tl: 부가 정보까지 출력` |

- **인자 이름 칸은 비워도 됩니다** — `{option}` `{job_id}` 순서로 자동 연결됩니다.
- 선택지는 한 칸 안에서 **줄바꿈(Alt+Enter)** 으로 하나씩, `값: 설명` 형태.
  **콜론 뒤에 공백**이 있어야 값과 설명이 갈립니다. 콤마로 나누지 마세요.

### 기본값을 비우면?

**그 인자가 커맨드에서 통째로 빠집니다.** 실제로 확인한 동작입니다.

| 기본값 | 에이전트가 준 값 | 실제 실행 |
|---|---|---|
| (비움) | 없음 | `phd list` |
| `-l` | 없음 | `phd list -l` |
| `-l` | `-lf` | `phd list -lf` |

자주 쓰는 형태가 있으면 기본값에 넣으세요. 에이전트가 값을 안 주면 그게 쓰입니다.

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
| 되돌리기를 눌러도 다르다고 나옴 | `admin-console` 재시작이 빠진 것입니다(1번) |
| 답변이 행을 줄여도 | 이제 **원문 블록**에 전체가 있습니다. 블록이 아예 없으면 `execution_raw_output` 확인 |
| 원문 블록이 너무 길다 | `execution_raw_output_min_lines`를 올리세요(예: 10) |
| 홈 경로를 지어냄 | 같음. `최신 지시문 OK`인데도 그러면 알려주세요 |
| 모델이 없는 옵션을 지어냄 | 4번에서 그 인자를 **선택형**으로 바꾸세요 |
| 모델 목록이 비고 질문이 401 | 콘솔 `agent_api_key`와 Open WebUI 연결 키가 다릅니다 |
| Service Hub curl 이 401 | 같은 키를 `Authorization: Bearer` 로 보내야 합니다 |
| 401인데 방금 키를 바꿨다 | 설정 캐시 5초입니다. 잠시 뒤 다시 |
| 멀쩡한 커맨드가 `다른 사용자의 자원…`으로 거부 | 설정 탭 `execution_user_scope_flags`에서 그 옵션을 빼세요 |
| 엑셀 업로드 후 일부 `건너뜀` | 사유가 화면에 나옵니다. 대개 인자 이름 불일치 — **이름 칸을 비우면** 자동으로 맞춰집니다 |
| `Expecting value: ...` 반복 | 설정 탭 `llm_streaming` → `false` → agent-server 재시작 |
| 커맨드가 `command not found` | 로그에 `로그인 셸이 필요합니다`가 있으면 자동 복구된 것. 계속되면 `.env`에 `SSH_PRIVDROP=su-login` |
| 되돌려야 할 때 | `DROP_EXISTING=yes bash scripts/restore-db.sh ../pg-backup-<날짜>.sql` |
