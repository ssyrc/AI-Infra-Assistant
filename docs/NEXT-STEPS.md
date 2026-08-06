# 지금 할 일

**[서버]** 202.20.183.30 · `/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant`
**[WSL]** `/home/yrc/AI-Infra-Assistant`
**[웹]** 관리자 콘솔 `http://202.20.183.30:8501` · Open WebUI `http://202.20.183.30:8502`

> ✅ **DB 복구 완료** (매뉴얼 5 · VOC 48,314 · 커맨드 3)
> ⛔ 며칠 정상 동작을 확인할 때까지 `docker volume prune` · `system prune` · `down -v` 금지.
> 남은 익명 볼륨 2개가 마지막 보험입니다(6번에서 정리).

---

## 1. [서버] 백업 — 이제부터 반영 전에 항상 먼저

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
bash scripts/backup-db.sh
```

## 2. [WSL] 코드 반영

```bash
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
bash /home/yrc/AI-Infra-Assistant/scripts/deploy-rsync.sh
```

## 3. [서버] 재시작

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
docker compose -f docker-compose.dev.yml restart admin-console agent-server
```

`admin-console`을 빼면 "기본 모델 지정 실패 401"이 그대로입니다(백엔드 코드가 안 바뀝니다).

## 4. [웹] `agent_api_key` 바로잡기

지금 `agent_api_key`에 **Open WebUI 관리자 키**가 들어가 있습니다. 두 키는 목적지가 반대라
그대로 두면 **사용자 질문이 전부 401**입니다. 아래 둘 중 하나로 맞추세요.

**(A) 인증을 켜서 쓴다 — 권장.** 아무 값이나 하나 정해 **양쪽에 같게** 넣습니다.

```bash
openssl rand -hex 24        # 나온 값을 아래 두 곳에 똑같이
```

| 넣을 곳 | 경로 |
|---|---|
| 콘솔 | 설정 탭 → 에이전트 → `agent_api_key` |
| Open WebUI | 관리자 패널 → 설정 → 연결(Connections) → `http://agent-server:8000/v1` 의 API 키 |

**(B) 임시로 끈다.** 콘솔에서 `agent_api_key`를 **비웁니다.** 인증이 사라지므로,
같은 망의 누구나 헤더만 바꿔 남의 계정으로 커맨드를 실행할 수 있습니다. 오래 두지 마세요.

확인:

```bash
docker compose -f docker-compose.dev.yml logs agent-server | grep "/v1/\*" | tail -2
```

- `[agent] /v1/* API 키 인증이 켜져 있습니다.` → (A)
- `[agent] !! /v1/* 에 인증이 없습니다. …` → (B)

## 5. [웹] 콘솔 설정 확인 + 지시문 되돌리기

`openwebui_admin_api_key`를 다시 저장해 보세요. **이번엔 401 없이 "기본 모델 지정" 까지
성공해야 합니다.** (이 값은 Open WebUI에서 발급받은 관리자 API 키입니다 —
Open WebUI 로그인 → 설정 → 계정 → API 키.)

값이 mock으로 돌아가 있으면 다시 넣습니다.

| key | 값 |
|---|---|
| `vllm_llm_base_url` / `vllm_llm_model` | `http://75.23.32.41:8000/v1` / `qwen3-235b-a22b` |
| `vllm_embed_base_url` / `vllm_embed_model` | `http://75.23.32.41:8010/v1` / `bge-m3` |
| `rerank_provider` / `rerank_base_url` / `rerank_model` | `vllm` / `http://75.23.32.41:8020/v1` / `bge-reranker-v2-m3` |
| `execution_host` | `202.20.185.100` |
| `openwebui_public_url` | `http://202.20.183.30:8502` |

그리고 **`지시문을 최신 기본값으로 되돌리기`** → `agent-server 재시작`.
이걸 안 누르면 6번의 3번 질문이 계속 가이드 문서를 안내합니다.

## 6. [웹] Open WebUI 동작 확인 — **소요 시간을 적어 주세요**

1. `S2 스케줄러 job list 확인해줘`
2. `내 홈 파일 리스트 보여줘`
3. `cocoa.song 계정이 어떤 gpu job 을 수행중이야?`

3번은 **한 줄**로만 나와야 합니다.

> 본인(yr9.choi) 자원만 조회할 수 있어 cocoa.song의 job은 확인할 수 없습니다.

`ops_assistant`라는 말이 나오거나 "가이드 위치: 슈퍼컴 Portal > …"이 붙으면 5번 지시문
되돌리기를 안 한 것입니다.

1·2번은 진행 줄을 **통째로** 보내주세요.

```
· `ls -lh` 실행하는 중
· 완료 (202.20.185.100 · yr9.choi · 0.4초) ⚠ 출력 132줄 중 58줄만
```

- 앞줄 = 실제로 실행된 커맨드. 숨김 파일이 없으면 여기에 `-A`가 없는 것입니다.
- `(202.20.185.100 · yr9.choi)` = 로그인 서버에서 본인 계정으로 돌았다는 증거입니다.
- `⚠ 출력 N줄 중 M줄만` = 잘린 것. 자주 보이면 `execution_result_max_chars`를 올리세요.

## 7. [웹] 커맨드 인자 다듬기 (엑셀)

콘솔 → 커맨드 실행 탭 → **`현재 등록분 내보내기`** → 엑셀에서 수정 → 업로드 →
**`execution-mcp 재시작`**.

지금 3개 모두 인자가 '문자열'인데 `{option}`은 **선택형**이 맞습니다.

| 이름 | 실행 커맨드 | 인자1 타입 | 인자1 선택지 |
|---|---|---|---|
| s2_phd_list | `phd list {option}` | 선택형 | `-l: 상세 정보를 길게 출력`<br>`-lf: 선택 가능한 필드 목록` |
| s2_phd_info_job_id | `phd info {option} {job_id}` | 선택형 | `-j: JSON 형식으로 반환`<br>`-tl: 부가 정보까지 출력` |

- **인자 이름 칸은 비워도 됩니다** — `{option}` `{job_id}` 순서로 자동 연결됩니다.
- 선택지는 한 칸 안에서 **줄바꿈(Alt+Enter)** 으로 하나씩, `값: 설명` 형태.
  **콜론 뒤에 공백**이 있어야 값과 설명이 갈립니다. 콤마로 나누지 마세요.

지금까지는 여기 적은 인자 설명이 에이전트에게 **전혀 전달되지 않았습니다**(#140에서 수정).
이제 전달되므로 옵션 선택이 눈에 띄게 좋아질 겁니다.

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
| 모델 목록이 비고 질문이 401 | 4번의 두 값이 다릅니다. 같은 문자열인지 확인 |
| "기본 모델 지정 실패 401" 계속 | 3번 `admin-console` 재시작을 빼먹은 것 |
| 401인데 방금 키를 바꿨다 | 설정 캐시 5초입니다. 잠시 뒤 다시 저장 |
| 멀쩡한 커맨드가 `다른 사용자의 자원…`으로 거부 | 설정 탭 `execution_user_scope_flags`에서 그 옵션을 빼세요(`sort -u` 등) |
| 엑셀 업로드 후 일부 `건너뜀` | 사유가 화면에 나옵니다. 대개 인자 이름 불일치 — **이름 칸을 비우면** 자동으로 맞춰집니다 |
| `Expecting value: ...` 반복 | 설정 탭 `llm_streaming` → `false` → agent-server 재시작 |
| 커맨드가 `command not found` | 로그에 `로그인 셸이 필요합니다`가 있으면 자동 복구된 것. 계속되면 `.env`에 `SSH_PRIVDROP=su-login` |
| 되돌려야 할 때 | `DROP_EXISTING=yes bash scripts/restore-db.sh ../pg-backup-<날짜>.sql` |
