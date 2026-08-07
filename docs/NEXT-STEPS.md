# 지금 할 일

**[서버]** 202.20.183.30 · `/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant`
**[WSL]** `/home/yrc/AI-Infra-Assistant`
**[웹]** 관리자 콘솔 `http://202.20.183.30:8501` · Open WebUI `http://202.20.183.30:8502`

> ⛔ 며칠 정상 동작을 확인할 때까지 `docker volume prune` · `system prune` · `down -v` 금지.
> 남은 익명 볼륨 2개가 마지막 보험입니다(맨 아래에서 정리).

**이번 핵심 (#155)** — 두 가지를 바꿨습니다.

1. **매뉴얼을 에이전트가 아니라 우리가 먼저 검색합니다.** 매 질문마다 시스템이 매뉴얼을
   검색해서 **문서 위치까지 포함한 근거**를 프롬프트에 넣어 줍니다. 모델이 "내가 아는 내용"이라
   판단해 검색을 건너뛰던 길 자체가 없어집니다.
2. **근거 없는 답변은 경고를 붙이는 게 아니라 통째로 버립니다.** 조회 결과에 없는 IP·경로가
   있으면 그 답변 대신 **운영팀 문의 안내**가 나갑니다.

> 문의하신 내용은 매뉴얼과 과거 사례에서 확인되지 않았습니다.
> 정확하지 않은 정보를 드리지 않기 위해 답변을 드리지 않습니다. 운영팀에 문의해 주세요.

그리고 지적하신 "콘솔에 reference path 다 있는데 안 쓴다" — **제 #154 진단이 틀렸습니다.**
데이터 문제가 아니라 툴 계약 문제였습니다. `search_manual`이 `guide_location`을 돌려주면서도
docstring에는 그 이름을 안 적어 둬서, 모델은 그런 필드가 있는 줄도 몰랐습니다. 고쳤습니다.

---

## 1. [서버] 코드 반영

```bash
# [서버]  반영 전 백업 먼저
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
bash scripts/backup-db.sh

# [WSL]
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
bash /home/yrc/AI-Infra-Assistant/scripts/deploy-rsync.sh

# [서버]  db-init 먼저 — 새 설정 키(manual_prefetch 등)가 생깁니다
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
docker compose -f docker-compose.dev.yml run --rm db-init
docker compose -f docker-compose.dev.yml restart admin-console agent-server manual-mcp
```

`manual-mcp`도 재시작해야 합니다(툴 설명이 바뀌었습니다). `admin-console`도 **필수**입니다 —
2번의 되돌리기 버튼이 옛 텍스트를 저장하는 것을 막습니다(#147 모듈 캐시).

## 2. [서버] 지시문을 최신으로

지시문이 바뀌었습니다. **반드시 되돌리기 버튼을 눌러야 반영됩니다**(non-force 시드).

콘솔 설정 탭 → **`지시문을 최신 기본값으로 되돌리기`** → 그다음 아래로 확인:

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
bash scripts/check-instruction.sh
docker compose -f docker-compose.dev.yml restart agent-server
```

```
코드 : 6437자  md5 ...
DB   : 6437자  md5 ...

✅ 최신입니다. DB의 지시문이 지금 코드와 같습니다.
```

`❌ 다릅니다`가 계속 나오면 `admin-console`을 재시작하고 다시 누르세요.

## 3. [웹] 확인 — Errors에 주신 질문 그대로

| 질문 | 기대 |
|---|---|
| `~~~ 접속이 안되면 슈퍼컴 접속 못 하는거 아니야?` | 매뉴얼 내용으로 답변. **다른 접속 가이드**가 나와야 합니다 |
| `login server 접속이 갑자기 안됩니다…` | 매뉴얼·과거 사례 기반. **없는 IP를 만들면 답변 자체가 운영팀 문의로 바뀝니다** |
| 다른 사용자 문의 (거절 대상) | 한 줄 거절. **가이드 안내가 붙으면 안 됩니다** |
| 문서를 안내하는 질문 | **가이드 위치 + 가이드 문서 두 줄**이 나와야 합니다 |

문서 안내는 이 형태여야 합니다(코드블록 아님).

자세한 내용은 다음 문서를 참고하세요:
 - 가이드 위치: 슈퍼컴 Portal > 활용 가이드 > 접속 가이드
 - 가이드 문서: 슈퍼컴퓨팅센터 사용자 매뉴얼

**위치 줄이 빠지면** 그 문서의 `reference_path`가 실제로 비어 있는 것입니다.
콘솔 → 매뉴얼 탭에서 그 문서만 채워 주세요.

### 서버 로그로 선검색이 도는지 확인

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
docker compose -f docker-compose.dev.yml logs --tail 200 agent-server | grep 선검색
```

`[agent] 매뉴얼 선검색 3건 (218ms)` 처럼 **질문마다 한 줄** 찍혀야 합니다.
`0건`만 계속 나오면 매뉴얼이 발행(published)되지 않았거나 임베딩 서버(:8010)를 못 부르는 것이니
그 줄을 보내주세요.

## 4. 달라진 체감 — 답변이 **한 번에** 나옵니다

근거 검사가 켜져 있으면 본문을 도중에 흘려보낼 수 없습니다(이미 화면에 나간 글자는 못 거둡니다).
그래서 **도구 진행 줄은 그대로 흐르고, 답변 본문만 끝나고 한 번에** 뜹니다.

글자가 흐르는 게 더 좋으시면 설정 탭에서 `answer_grounding_check` → `false`.
**대신 지어낸 값이 그대로 나갑니다.** 권장하지 않습니다.

### 이번에 생긴 설정 (설정 탭)

| key | 기본 | 뜻 |
|---|---|---|
| `manual_prefetch` | `true` | 매 질문마다 매뉴얼 선검색 |
| `manual_prefetch_top_k` | `3` | 프롬프트에 넣을 근거 문단 수 |
| `answer_grounding_check` | `true` | 근거 없는 답변을 운영팀 문의로 교체 |

## 5. [웹] 커맨드 인자를 **선택형**으로 (아직 안 하셨으면)

`phd list -a`의 `-a`는 모델이 지어낸 옵션입니다. 인자가 '문자열'이라 뭐든 들어갑니다.
**선택형으로 바꾸면 스키마에 값이 박혀 지어낼 수 없습니다.**

콘솔 → 커맨드 실행 탭 → **`현재 등록분 내보내기`** → 엑셀 수정 → 업로드 →
**`execution-mcp 재시작`**.

타입 · 필수 · 활성 · 실행 위치는 셀을 클릭하면 드롭다운이 나옵니다.

| 이름 | 실행 커맨드 | 인자1 타입 | 인자1 선택지 |
|---|---|---|---|
| s2_phd_list | `phd list {option}` | 선택형 | `-l: 상세 정보를 길게 출력`<br>`-lf: 선택 가능한 필드 목록` |
| s2_phd_info_job_id | `phd info {option} {job_id}` | 선택형 | `-j: JSON 형식으로 반환`<br>`-tl: 부가 정보까지 출력` |

- 인자 이름 칸은 비워도 됩니다 — `{option}` `{job_id}` 순서로 자동 연결됩니다.
- 선택지는 한 칸 안에서 **줄바꿈(Alt+Enter)**, `값: 설명` 형태. **콜론 뒤 공백 필수.**
  콤마로 나누지 마세요.
- 기본값을 비우면 그 인자는 커맨드에서 통째로 빠집니다(`phd list`).

## 6. [서버] Service Hub 연동 (아직 안 하셨으면)

`docs/SERVICE-HUB.md` 를 Service Hub 팀에 전달하세요. 먼저 이 curl 결과를 보내주세요.

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
`service_hub_mcp_url`에 넣으세요. 비워 두면 그 부분만 생략됩니다.

---

## 며칠 뒤 (정상 동작 확인 후)

```bash
docker volume rm 553dd7066a559e45d37bb0d7d7d4b47fadeff60309477e7b9a8ebe0d6a769448
docker volume rm 1dc7527fd826d5a2afc08bd1b44e945219c2fd10da65c2747f49c2d367ab9198
```

## 문제가 생기면

| 증상 | 조치 |
|---|---|
| 멀쩡한 질문에도 "운영팀에 문의" | 그 질문과 답을 보내주세요. 검사가 과하게 잡는 것이니 규칙을 좁힙니다 |
| 답변이 한 번에 떠서 답답하다 | 4번. `answer_grounding_check` → `false` (지어내기 방지가 꺼집니다) |
| 로그에 `매뉴얼 선검색 실패` | 임베딩 서버(:8010) 또는 manual_db 문제. 그 줄을 보내주세요 |
| `ContextWindowExceededError` | `manual_prefetch_top_k` 를 `2`로 내리세요 |
| 문서 위치 줄이 안 나옴 | 콘솔 매뉴얼 탭에서 그 문서의 **문서 위치**를 채우세요 |
| 되돌리기를 눌러도 다르다고 나옴 | `admin-console` 재시작이 빠진 것입니다(1번) |
| 답변이 행을 줄임 | **원문 블록**에 전체가 있습니다. 블록이 없으면 `execution_raw_output` 확인 |
| 원문 블록이 너무 길다 | `execution_raw_output_min_lines`를 올리세요(예: 10) |
| 모델이 없는 옵션을 지어냄 | 5번에서 그 인자를 **선택형**으로 |
| 모델 목록이 비고 질문이 401 | 콘솔 `agent_api_key`와 Open WebUI 연결 키가 다릅니다 |
| 401인데 방금 키를 바꿨다 | 설정 캐시 5초입니다. 잠시 뒤 다시 |
| 멀쩡한 커맨드가 `다른 사용자의 자원…`으로 거부 | 설정 탭 `execution_user_scope_flags`에서 그 옵션을 빼세요 |
| 엑셀 업로드 후 일부 `건너뜀` | 사유가 화면에 나옵니다. 대개 인자 이름 불일치 — **이름 칸을 비우면** 자동으로 맞춰집니다 |
| `Expecting value: ...` 반복 | 설정 탭 `llm_streaming` → `false` → agent-server 재시작 |
| 커맨드가 `command not found` | 계속되면 `.env`에 `SSH_PRIVDROP=su-login` |
| 되돌려야 할 때 | `DROP_EXISTING=yes bash scripts/restore-db.sh ../pg-backup-<날짜>.sql` |
