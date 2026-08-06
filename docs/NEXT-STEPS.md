# 지금 할 일

**[서버]** 202.20.183.30 · `/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant`
**[WSL]** `/home/yrc/AI-Infra-Assistant`
**[웹]** 관리자 콘솔 `http://202.20.183.30:8501` · Open WebUI `http://202.20.183.30:8502`

> ✅ **DB 복구 완료** (매뉴얼 5 · VOC 48,314 · 커맨드 3)
> ⛔ 며칠 정상 동작을 확인할 때까지 `docker volume prune` · `system prune` · `down -v` 금지.
> 남은 익명 볼륨 2개가 마지막 보험입니다(맨 아래에서 정리).

---

## 1. [서버] 백업 후 코드 반영 — **인증 구멍 하나를 더 막았습니다**

`/v1/memory/{user_id}` 세 개(GET/POST/DELETE)에 인증이 빠져 있었습니다(#143). 특히 POST는
**남의 에이전트에 영구 지시를 심을 수 있어** 위험합니다. 지금 반영해 주세요.

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

## 2. 키를 새로 만들어 **양쪽에** 넣기

`agent_api_key`는 **우리가 정하는 값**입니다. 기존 값을 읽으려 하지 마세요 —
`is_secret=true`라 콘솔에서는 뒤 4자만 보이고(`••••••••a1b2`), DB에서 꺼내는 것도 번거롭습니다.
어차피 양쪽 다 우리가 정하므로 **새로 만들어 덮어쓰는 게 가장 빠릅니다.**

```bash
# [아무 데서나] 키 생성 — 이 값을 메모장에 복사해 두세요
openssl rand -hex 24
```

이 **하나의 값**을 두 곳에 똑같이 붙여넣습니다.

| # | 넣을 곳 | 경로 |
|---|---|---|
| ① | 관리자 콘솔 | 설정 탭 → 에이전트 → `agent_api_key` → 저장 |
| ② | Open WebUI | 관리자 패널 → 설정 → **연결(Connections)** → `http://agent-server:8000/v1` → **API 키** |

②에서 URL이 아직 없으면 새로 추가하세요. URL은 `http://agent-server:8000/v1` 입니다
(사용자 접속 주소 8502가 아니라 **도커 내부 주소**입니다).

저장 후 Open WebUI의 **연결 테스트**가 통과해야 합니다.

### 두 값이 같은지 확인하는 법

콘솔은 뒤 4자를 보여줍니다. 아래로 길이와 뒤 4자를 확인해 눈으로 맞춰 보세요(값 자체는
찍지 않습니다).

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
docker compose -f docker-compose.dev.yml exec -T postgres psql -U agent -d platform_config -c \
  "select key, length(value) as 길이, right(value,4) as 끝4자
     from platform_settings where key in ('agent_api_key','openwebui_admin_api_key');"
```

`openssl rand -hex 24`는 **길이 48**이 나옵니다. 그게 아니면 잘못 붙여넣은 것입니다.

## 3. [서버] 모델이 보이는지 확인

Open WebUI 컨테이너 **안에서** agent-server를 직접 불러 봅니다. 여기서 갈립니다.

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant

echo -n "키 없이 : "
docker compose -f docker-compose.dev.yml exec -T open-webui \
  curl -s -o /dev/null -w '%{http_code}\n' http://agent-server:8000/v1/models

echo -n "키 넣고 : "
docker compose -f docker-compose.dev.yml exec -T open-webui \
  curl -s -H "Authorization: Bearer <2번에서_만든_키>" http://agent-server:8000/v1/models
```

- `키 없이 = 401`, `키 넣고 = {"object":"list","data":[...]}` → **정상**입니다.
  그래도 화면에 모델이 안 보이면 아래 두 가지를 하세요.
- 둘 다 실패하면 결과를 보내주세요(연결 자체의 문제입니다).

모델이 여전히 안 보일 때:

- 관리자 패널 → **모델** → 해당 모델 → **공개범위(Visibility)를 `Public`** 으로.
  이걸 안 하면 admin에게만 보이거나 아무에게도 안 보입니다.
- 브라우저 **하드 새로고침**(Ctrl+Shift+R).

## 4. [서버] Service Hub 연동 — 규격서를 만들어 뒀습니다

`docs/SERVICE-HUB.md` 를 Service Hub 팀에 전달하세요. 요약하면:

**Service Hub → 우리** (`agent_api_key` 필요)

```bash
KEY=<agent_api_key>
curl -s -X POST http://202.20.183.30:8500/v1/voc/query \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"voc_info":{"voc_id":"TEST-1","voc_title":"테스트",
       "requester":{"user_id":"yr9.choi"},
       "voc_content":{"text":"내 GPU job 목록 알려줘"}},
       "output_option":"markdown"}' | python3 -m json.tool
```

이 curl이 서버에서 되는지 먼저 확인하고 결과를 보내주세요.

**우리 → Service Hub** (유사 VOC 조회, 선택)
Service Hub MCP 주소를 받아 콘솔 설정 탭 `service_hub_mcp_url`에 넣으면 답변에
`similar_voc`가 붙습니다. 비워 두면 그 부분만 생략되고 나머지는 정상 동작합니다.

## 5. [웹] Open WebUI 동작 확인 — **소요 시간을 적어 주세요**

1. `S2 스케줄러 job list 확인해줘`
2. `내 홈 파일 리스트 보여줘`
3. `cocoa.song 계정이 어떤 gpu job 을 수행중이야?`

3번은 **한 줄**로만 나와야 합니다.

> 본인(yr9.choi) 자원만 조회할 수 있어 cocoa.song의 job은 확인할 수 없습니다.

`ops_assistant`라는 말이 나오거나 "가이드 위치: 슈퍼컴 Portal > …"이 붙으면 콘솔 설정 탭의
**지시문을 최신 기본값으로 되돌리기**를 안 누른 것입니다.

1·2번은 진행 줄을 **통째로** 보내주세요.

```
· `ls -lh` 실행하는 중
· 완료 (202.20.185.100 · yr9.choi · 0.4초) ⚠ 출력 132줄 중 58줄만
```

- 앞줄 = 실제로 실행된 커맨드. 숨김 파일이 없으면 여기에 `-A`가 없는 것입니다.
- `(202.20.185.100 · yr9.choi)` = 로그인 서버에서 본인 계정으로 돌았다는 증거입니다.
- `⚠ 출력 N줄 중 M줄만` = 잘린 것. 자주 보이면 `execution_result_max_chars`를 올리세요.

## 6. [웹] 커맨드 인자 다듬기 (엑셀)

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
| 모델 목록이 비고 질문이 401 | 2번의 두 곳에 넣은 값이 다릅니다. 길이(48)와 끝 4자로 확인 |
| Service Hub curl 이 401 | 같은 키를 `Authorization: Bearer` 로 보내야 합니다 |
| "기본 모델 지정 실패 401" 계속 | `admin-console` 재시작을 빼먹은 것(`restart admin-console`) |
| 401인데 방금 키를 바꿨다 | 설정 캐시 5초입니다. 잠시 뒤 다시 저장 |
| 멀쩡한 커맨드가 `다른 사용자의 자원…`으로 거부 | 설정 탭 `execution_user_scope_flags`에서 그 옵션을 빼세요(`sort -u` 등) |
| 엑셀 업로드 후 일부 `건너뜀` | 사유가 화면에 나옵니다. 대개 인자 이름 불일치 — **이름 칸을 비우면** 자동으로 맞춰집니다 |
| `Expecting value: ...` 반복 | 설정 탭 `llm_streaming` → `false` → agent-server 재시작 |
| 커맨드가 `command not found` | 로그에 `로그인 셸이 필요합니다`가 있으면 자동 복구된 것. 계속되면 `.env`에 `SSH_PRIVDROP=su-login` |
| 되돌려야 할 때 | `DROP_EXISTING=yes bash scripts/restore-db.sh ../pg-backup-<날짜>.sql` |
