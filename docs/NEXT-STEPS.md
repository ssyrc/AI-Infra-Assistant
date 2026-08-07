# 지금 할 일

**[서버]** 202.20.183.30 · `/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant`
**[WSL]** `/home/yrc/AI-Infra-Assistant`
**[웹]** 관리자 콘솔 `http://202.20.183.30:8501` · Open WebUI `http://202.20.183.30:8502`

> ⛔ 며칠 정상 동작을 확인할 때까지 `docker volume prune` · `system prune` · `down -v` 금지.
> 남은 익명 볼륨 2개가 마지막 보험입니다(맨 아래에서 정리).

**이번 핵심 (#155 + #156)** — 아직 서버에 안 올라간 #155까지 한 번에 반영됩니다.

1. **매뉴얼과 VOC를 에이전트가 아니라 우리가 먼저 검색합니다.** 매 질문마다 시스템이 둘 다
   (동시에) 검색해서 근거를 프롬프트에 넣습니다. 모델이 "내가 아는 내용"이라 판단해 검색을
   건너뛰던 길 자체가 없어집니다.
2. **이어지는 질문도 검색이 됩니다.** `그러면 슈퍼컴 접속 못 하는거 아니야?`에는 검색할 명사가
   없어서 아무것도 안 나왔습니다. 이제 **직전 사용자 질문**을 붙여서 검색합니다.
3. **운영팀 문의는 맨 마지막입니다.** 매뉴얼에서 확인된 "먼저 해 보실 것"을 다 안내한 뒤에
   운영팀으로 넘깁니다. 지어낸 값이 섞여 있으면 **그 줄만** 빼고 나머지는 그대로 나갑니다.
4. **낯선 클러스터 이름을 보고 "우리 소관이 아니다"라고 답하는 길을 막았습니다.**

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

# [서버]  db-init 먼저 — 새 설정 키(rag_prefetch 등)가 생깁니다
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
docker compose -f docker-compose.dev.yml run --rm db-init
bash scripts/restart-mounted.sh
```

이번에는 `restart-mounted.sh`로 **전부** 재시작하세요. manual-mcp(툴 설명)·voc-mcp(검색 코드가
`shared/`로 이동)·agent-server·admin-console이 모두 바뀌었습니다. 이미지 재빌드는 필요 없습니다.

## 2. [서버] 지시문을 최신으로

지시문이 바뀌었습니다. **되돌리기 버튼을 눌러야 반영됩니다**(non-force 시드).

콘솔 설정 탭 → **`지시문을 최신 기본값으로 되돌리기`** → 그다음:

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
bash scripts/check-instruction.sh
docker compose -f docker-compose.dev.yml restart agent-server
```

```
코드 : 6977자  md5 ...
DB   : 6977자  md5 ...

✅ 최신입니다. DB의 지시문이 지금 코드와 같습니다.
```

`❌ 다릅니다`가 계속 나오면 `admin-console`을 재시작하고 다시 누르세요.

## 3. [서버] 선검색이 도는지 **먼저** 확인 — 여기서 갈립니다

질문을 하나 던진 뒤:

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
docker compose -f docker-compose.dev.yml logs --tail 200 agent-server | grep 선검색
```

**질문마다 한 줄** 이렇게 찍혀야 합니다.

```
[agent] 선검색 매뉴얼 3건 · VOC 2건 (241ms)
```

| 보이는 것 | 뜻 / 다음 |
|---|---|
| 줄이 아예 없음 | 코드가 안 올라갔거나 `rag_prefetch`가 꺼짐. 1번을 다시 |
| `매뉴얼 0건`이 계속 | 매뉴얼이 **발행(published)** 안 됐거나 임베딩(:8010) 문제 |
| `VOC 0건`이 계속 | voc_records 임베딩이 비었을 수 있습니다. 그 줄을 보내주세요 |
| `선검색 실패` | 그 줄 전체를 보내주세요 |

**이게 안 나오면 아래 4번은 볼 필요 없습니다.** 원인이 여기입니다.

## 4. [웹] 확인 — Errors에 주신 시나리오 그대로

세 질문을 **한 대화에서 이어서** 하세요. 2번이 이번에 고친 핵심입니다.

| # | 질문 | 기대 |
|---|---|---|
| 1 | `login server 접속이 갑자기 안됩니다. 오전에는 잘되었는데…` | **매뉴얼 기반 확인 사항을 먼저 쭉** 안내한 뒤, 마지막에 운영팀 문의 |
| 2 | 이어서 `그러면 슈퍼컴 접속 못 하는거 아니야?` | 다른 **접속 가이드**가 나와야 합니다(직전 질문 문맥으로 검색) |
| 3 | voc_db에 있는 질문을 **그대로** 복사해서 | 그 VOC의 answer 내용으로 답해야 합니다 |
| 4 | `~~ 클러스터` 관련 질문 | "우리 소관이 아니다"라고 하면 **안 됩니다** |
| 5 | 다른 사용자 문의 (거절 대상) | 한 줄 거절. 가이드 안내가 붙으면 안 됩니다 |

문서 안내는 이 형태여야 합니다(코드블록 아님).

자세한 내용은 다음 문서를 참고하세요:
 - 가이드 위치: 슈퍼컴 Portal > 활용 가이드 > 접속 가이드
 - 가이드 문서: 슈퍼컴퓨팅센터 사용자 매뉴얼

**위치 줄이 빠지면** 그 문서의 `reference_path`가 비어 있는 것입니다 —
콘솔 → 매뉴얼 탭에서 그 문서만 채워 주세요.

### 안 되는 질문이 있으면 이렇게 보내주세요

질문 원문 + 답변 전체 + 그때의 `선검색` 로그 한 줄. 셋이 있어야
**검색이 못 찾은 것인지(RAG 문제) · 찾았는데 모델이 안 쓴 것인지(지시문 문제)** 가릅니다.
지금까지는 이 둘을 못 갈라서 지시문만 계속 고쳤습니다.

## 5. 달라진 체감 — 답변이 **한 번에** 나옵니다

근거 검사가 켜져 있으면 본문을 도중에 흘려보낼 수 없습니다(이미 나간 글자는 못 거둡니다).
그래서 **도구 진행 줄은 그대로 흐르고, 답변 본문만 끝나고 한 번에** 뜹니다.

글자가 흐르는 게 더 좋으시면 설정 탭 `answer_grounding_check` → `false`.
**대신 지어낸 값이 그대로 나갑니다.** 권장하지 않습니다.

### 이번에 생긴 설정 (설정 탭)

| key | 기본 | 뜻 |
|---|---|---|
| `rag_prefetch` | `true` | 매 질문마다 매뉴얼+VOC 선검색 |
| `manual_prefetch_top_k` | `3` | 프롬프트에 넣을 매뉴얼 근거 수 |
| `voc_prefetch_top_k` | `3` | 프롬프트에 넣을 과거 사례 수 |
| `answer_grounding_check` | `true` | 근거 없는 줄을 덜어냄(전부면 운영팀 문의로 교체) |

## 6. [웹] 커맨드 인자를 **선택형**으로 (아직 안 하셨으면)

`phd list -a`의 `-a`는 모델이 지어낸 옵션입니다. 인자가 '문자열'이라 뭐든 들어갑니다.
**선택형으로 바꾸면 스키마에 값이 박혀 지어낼 수 없습니다.**

콘솔 → 커맨드 실행 탭 → **`현재 등록분 내보내기`** → 엑셀 수정 → 업로드 →
**`execution-mcp 재시작`**.

| 이름 | 실행 커맨드 | 인자1 타입 | 인자1 선택지 |
|---|---|---|---|
| s2_phd_list | `phd list {option}` | 선택형 | `-l: 상세 정보를 길게 출력`<br>`-lf: 선택 가능한 필드 목록` |
| s2_phd_info_job_id | `phd info {option} {job_id}` | 선택형 | `-j: JSON 형식으로 반환`<br>`-tl: 부가 정보까지 출력` |

- 인자 이름 칸은 비워도 됩니다 — `{option}` `{job_id}` 순서로 자동 연결됩니다.
- 선택지는 한 칸에서 **줄바꿈(Alt+Enter)**, `값: 설명` 형태. **콜론 뒤 공백 필수.** 콤마 금지.
- 기본값을 비우면 그 인자는 커맨드에서 통째로 빠집니다(`phd list`).

## 7. [서버] Service Hub 연동 (아직 안 하셨으면)

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
| 여전히 매뉴얼을 안 쓰는 것 같다 | **3번 로그 먼저**. `선검색` 줄이 없으면 코드가 안 올라간 것입니다 |
| 멀쩡한 질문에도 "운영팀에 문의" | 질문·답변·선검색 로그 세 개를 보내주세요 |
| 답변 끝에 `확인되지 않은 내용은 제외했습니다` | 지어낸 값이 든 줄을 뺀 것입니다. 그 답변을 보내주세요 |
| 답변이 한 번에 떠서 답답하다 | 5번. `answer_grounding_check` → `false` (지어내기 방지가 꺼집니다) |
| `ContextWindowExceededError` | `manual_prefetch_top_k`·`voc_prefetch_top_k` 를 `2`로 |
| 답변이 느려졌다 | 선검색이 붙은 만큼(0.2~0.4초)입니다. 로그의 ms를 보내주세요 |
| 문서 위치 줄이 안 나옴 | 콘솔 매뉴얼 탭에서 그 문서의 **문서 위치**를 채우세요 |
| 되돌리기를 눌러도 다르다고 나옴 | `admin-console` 재시작이 빠진 것입니다(1번) |
| 답변이 행을 줄임 | **원문 블록**에 전체가 있습니다. 없으면 `execution_raw_output` 확인 |
| 원문 블록이 너무 길다 | `execution_raw_output_min_lines`를 올리세요(예: 10) |
| 모델이 없는 옵션을 지어냄 | 6번에서 그 인자를 **선택형**으로 |
| 모델 목록이 비고 질문이 401 | 콘솔 `agent_api_key`와 Open WebUI 연결 키가 다릅니다 |
| 401인데 방금 키를 바꿨다 | 설정 캐시 5초입니다. 잠시 뒤 다시 |
| 멀쩡한 커맨드가 `다른 사용자의 자원…`으로 거부 | 설정 탭 `execution_user_scope_flags`에서 그 옵션을 빼세요 |
| 엑셀 업로드 후 일부 `건너뜀` | 사유가 화면에 나옵니다. 대개 인자 이름 불일치 — **이름 칸을 비우면** 자동 |
| `Expecting value: ...` 반복 | 설정 탭 `llm_streaming` → `false` → agent-server 재시작 |
| 커맨드가 `command not found` | 계속되면 `.env`에 `SSH_PRIVDROP=su-login` |
| 되돌려야 할 때 | `DROP_EXISTING=yes bash scripts/restore-db.sh ../pg-backup-<날짜>.sql` |
