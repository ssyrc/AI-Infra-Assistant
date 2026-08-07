# AI Infra Assistant — 작업 컨텍스트

세션이 바뀌어도 이어서 작업할 수 있도록, **매 세션 시작 시 아래를 반드시 먼저 읽는다.**

## 0. 세션 시작 시 필독 (순서대로)

1. **`docs/HISTORY.md` 전체** — 지금까지의 모든 작업/원인분석 기록(#1부터 최신까지). 일부만
   읽지 말 것. 이전 세션에서 이미 원인을 밝힌 문제를 다시 추측하지 않기 위한 유일한 기억이다.
   `docs/RUN-LOG.md`는 **서버 기동·배포 절차만** 담는다(원인분석은 HISTORY로).
2. **`Errors`** — 사용자가 회사 서버에서 겪은 최신 에러/요청을 붙여넣는 파일. 이번 턴의 과제.
3. **`docs/NEXT-STEPS.md`** — 사용자가 지금 해야 할 일.
4. `Changes` — 사용자가 서버에서 직접 수정한 내역(사내 pip 미러 대응 등).

## 1. 작업 루프 (사용자와의 약속)

- 사용자가 `Errors`에 에러/요청을 업데이트 → 코드 수정 → 문서 갱신 → **`main`에 바로 커밋·푸시**
  (별도 브랜치·PR 만들지 않는다).
- `docs/HISTORY.md`: 원인분석/조치 내역을 번호 이어서 추가. 여기가 기억 저장소다.
- `docs/RUN-LOG.md`: 기동·배포 절차가 바뀔 때만 갱신(작업 일지가 아니다).
- `docs/NEXT-STEPS.md`: **사용자가 할 일만 간결하게.** 설계 설명·부연 금지(그건 RUN-LOG로).
  단, **"재시작하세요" 같은 말로 끝내지 말고 실행할 커맨드를 그대로 적는다.** 각 단계에
  실행 위치([WSL]/[서버]/[웹])와 작업 디렉토리를 명시한다. 사용자는 커맨드를 복사해서 붙여넣는다.
  · **사용자가 할 일이 생기면 채팅으로만 주지 말고 반드시 이 파일에 반영한다**(사용자 요청).
    채팅은 스크롤로 사라진다 — 서버 앞에서 보는 것은 이 파일 하나다. 답변에 커맨드를 적었다면
    그 턴에 NEXT-STEPS도 같이 고친다. 예외 없다.
  · **매번 처음부터 다시 쓴다.** 이미 끝난 단계는 지우고 지금 할 것만 남긴다
    (끝난 절차가 남아 있으면 사용자가 그걸 또 실행한다 — #141이 그렇게 났다).
- **커밋 전 `bash scripts/verify.sh`** (테스트+lint+셸 문법). `pytest … | tail && git commit`으로
  묶지 말 것 — **파이프가 종료코드를 가려** 실패한 테스트를 그대로 푸시한 일이 두 번 있다(#146·#152).
- 답변/문서/커밋 메시지는 한국어.
- 사용자는 폐쇄망 서버에서 직접 명령을 실행한다. 우리가 서버에 접속할 수 없으므로,
  실행할 커맨드를 정확히 주고 결과를 받아서 판단한다.

## 2. 배포 토폴로지

**GitHub은 폐쇄망에서 안 닿는다.** 코드는 항상 WSL을 거쳐 rsync로 들어간다.

| 위치 | 주소 / 경로 | 역할 |
|---|---|---|
| WSL (인터넷 O) | `/home/yrc/AI-Infra-Assistant` | GitHub pull, 이미지 빌드, 휠 다운로드 |
| 게이트/로그인 서버 | 202.20.185.100 (`login07`), `/home/gpu1/yr9.choi/05_halo/` | rsync 수신 지점. 내부 서버로 자동 라우팅. ssh 키 등록 완료 |
| 폐쇄망 배포 호스트 | 202.20.183.30, `05_halo/AI-Infra-Assistant` | 도커 컨테이너 전부 여기 |
| GPU 서버 | 75.23.32.41 (`hgpu4041`) | vLLM LLM :8000 / 임베딩 :8010 / 리랭커 :8020 |
| 포트 | agent :8500 · 관리자 콘솔 :8501 · 사용자 웹(Open WebUI) :8502 · 차트 이미지 :8509 |

- 모델: LLM `Qwen3-235B-A22B-Instruct-2507-FP8`(TP=4, `--max-model-len 32768`,
  `--enable-auto-tool-choice --tool-call-parser hermes` 필수), 임베딩 `bge-m3`(1024차원 —
  DB 스키마가 `vector(1024)` 고정), 리랭커 `bge-reranker-v2-m3`.

## 2-1. 반영 절차 — 매번 이대로 안내한다

**(A) 코드만 바뀐 경우** (requirements/Dockerfile 변경 없음) — 대부분 여기 해당. 재빌드 불필요.
`docker-compose.dev.yml`이 `./mcp_servers`·`./shared`를 마운트하므로 재시작만으로 반영된다.

```bash
# 폐쇄망 배포 호스트(202.20.183.30) — **반영 전에 항상 백업 먼저**(#141)
bash scripts/backup-db.sh

# WSL
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
bash scripts/deploy-rsync.sh      # 제외 목록이 스크립트에 박혀 있다. 삭제될 파일을 먼저 보여준다

# 폐쇄망 배포 호스트
docker compose -f docker-compose.dev.yml run --rm db-init   # 마이그레이션/새 설정 키가 있을 때만
bash scripts/restart-mounted.sh
```

**rsync 커맨드를 손으로 치지 않는다** — `--delete`가 저장소에 없는 파일(`.env`, ssh 키)을
지운 사고가 있었다(#137). 제외 목록은 `scripts/deploy-rsync.sh`에만 둔다. 서버에 새 상태를
두게 되면 그 스크립트의 `EXCLUDES`에 추가할 것.
되돌리기는 `bash scripts/restore-db.sh <덤프>`(기존 DB가 있으면 `DROP_EXISTING=yes` 필요).

**(B) requirements/Dockerfile이 바뀐 경우** — 이미지를 다시 만들어 tar로 옮겨야 한다.

```bash
# WSL: 빌드 → 저장 → 전송
bash scripts/rebuild.sh                    # 순차 빌드(병렬 금지 — 미러가 간헐적으로 빈 응답)
TAG=main-$(git rev-parse --short HEAD) bash scripts/save-runtime-images.sh
rsync -avz --progress dist/ai-infra-assistant-runtime-<TAG>.tar \
  yr9.choi@202.20.185.100:/home/gpu1/yr9.choi/05_halo/

# 폐쇄망: 로드 → 재태깅 → 기동
docker load < ai-infra-assistant-runtime-<TAG>.tar
bash scripts/retag-runtime-images.sh <TAG>     # ellie0/*:<TAG> → compose가 찾는 로컬 태그
docker compose -f docker-compose.dev.yml up -d --no-build
```
재태깅을 빼먹으면 `No such image: ai-infra-assistant-admin-console:latest`가 난다(RUN-LOG #2).

- 사내 pip 미러(Nexus)가 패키지를 간헐적으로 `from versions: none`으로 뱉는다 →
  WSL에서 `pip download <pkg>==<ver> -d vendor/`로 받아 `vendor/*.whl`에 넣는다
  (Dockerfile이 vendor의 모든 휠을 먼저 오프라인 설치 — Dockerfile 수정 불필요).
  미러에 어떤 버전이 있는지는 `bash scripts/debug-now.sh <pkg>==<ver>`로 확인.
- 내부 미러도 프록시(`202.20.187.241:3128`) 경유 필수.

## 2-2. 운영 설정값 (관리자 콘솔 설정 탭)

설정이 날아갔을 때 이 값으로 복구한다. 실제 서빙 중인 이름은
`curl http://75.23.32.41:8000|8010|8020/v1/models`로 확인하는 게 가장 확실하다.

| key | 값 |
|---|---|
| `vllm_llm_base_url` | `http://75.23.32.41:8000/v1` |
| `vllm_llm_model` | `qwen3-235b-a22b` (`--served-model-name`으로 준 이름) |
| `vllm_embed_base_url` | `http://75.23.32.41:8010/v1` |
| `vllm_embed_model` | `bge-m3` |
| `rerank_provider` | `vllm` (vLLM으로 리랭커를 띄웠을 때. TEI면 `tei`) |
| `rerank_base_url` | `http://75.23.32.41:8020/v1` (`tei`면 `/v1` 없이) |
| `rerank_model` | `bge-reranker-v2-m3` |
| `execution_host` | `202.20.185.100` (**이름 금지 — IP로**. login07은 /etc/hosts에서 75.11.29.7로 풀려 전부 실패했다. 구 `scheduler_login_host`, #128에서 개명) |
| `openwebui_public_url` | `http://202.20.183.30:8502` (사용자 접속 주소. 콘솔이 API를 부르는 `openwebui_base_url`(8080)과 다르다) |
| `agent_system_instruction` | 콘솔 설정 탭 **"지시문을 최신 기본값으로 되돌리기"** 버튼(#136) |

- **왜 초기화되나**: `docker-compose.dev.yml`의 `dev-config` 서비스가 `up` 할 때마다 vLLM 주소를
  mock으로 되돌린다. 콘솔에서 저장한 값(`updated_by`가 관리자 계정)과 `.env`에 실제 주소를 넣은
  환경은 건드리지 않도록 이중 가드를 걸어뒀다(RUN-LOG #28, #48). 그래도 **postgres 볼륨이
  삭제되면** 설정·매뉴얼·VOC가 전부 사라진다 — `down -v`·`volume prune`은 쓰지 말 것.
- **compose에서 컨테이너를 재생성시키는 변경(`ports`·`environment`·`image`·`command`)을 할 때는
  그 서비스에 이름 있는 볼륨이 붙어 있는지 먼저 확인한다**(#141). dev postgres에는 볼륨이 아예
  없어서 `ports:` 한 줄을 고친 것만으로 DB가 통째로 날아갔다. `down -v`만 위험한 게 아니다.
  회귀 테스트 `test_postgres_has_named_data_volume`가 dev/prod 양쪽을 고정한다.
- **반영 작업 전에는 `pg_dumpall` 백업을 먼저 돌린다**(NEXT-STEPS 7번에 커맨드).
- 실서버의 `.env`에 `VLLM_LLM_BASE_URL`/`VLLM_EMBED_BASE_URL`/`RERANK_*`/`EXECUTION_HOST`를
  실제 값으로 넣어두면 DB를 새로 만들어도 시드 단계에서 올바른 값이 들어간다.

## 3. 절대 규칙

- **커맨드는 어떤 경우에도 root로 실행하지 않는다.** 모든 실행은
  `shared/ssh_exec.run_ssh_as_user()` 하나만 거치며, 항상 `ssh root@host` → 호출자 본인 계정으로
  강등해 실행한다. 우회 경로를 만들지 않는다.
  강등 방식은 `SSH_PRIVDROP`으로 고른다(`su-login` 기본 / `su` / `runuser`) — 셋 다 본인 계정으로
  내려가는 것은 같고, 커맨드당 고정 비용만 다르다(#134). **사용자별 셸을 상주시키지 않는다**:
  파이프에 커맨드 문자열을 써 넣어야 해서 "셸 미사용" 원칙이 깨진다(#103, #134).
- 셸을 쓰지 않는다(argv 리스트로 실행). 파괴적 명령은 등록/실행 단계에서 거부한다.
- `user_id`는 LLM 스키마에서 숨기고 호출자 헤더(`X-User-Id`)에서 강제 주입한다(남의 자원 접근 불가).
- **남의 계정을 지목하는 인자도 막는다**(#140). 위 규칙은 *실행 신원*만 고정한다 —
  `phd list -u cocoa.song`은 본인 계정으로 돌려도 남의 job을 보여준다(판정 주체가 OS가 아니라
  그 프로그램이다). `execution_exec.reject_other_user()`가 `-u 남`·`--user=남`·`-u남`·등록
  커맨드 자리표시자 네 경로를 실행 직전에 끊는다. 옵션 목록은 `execution_user_scope_flags`.
  **"모델이 거절하니까 괜찮다"로 두지 않는다** — 그건 강제가 아니다.
- 답변에서 사용자를 부를 때 쓸 계정은 `build_agent`가 '이 환경의 값'에 넣어 준다.
  이게 없으면 모델이 자기 이름(`ops_assistant`)을 사용자 계정으로 말한다(#125·#131·#140).
- **지시문에 목록을 만들지 않는다**(사용자 지시, #145·#149). 커맨드 이름도, 질문 형태도
  마찬가지다("보여줘/어디야/얼마야" 나열 → 목록에 없는 표현이 오면 또 뚫린다).
  **판별 기준을 주고 모델이 적용하게 한다** — 예: "그 답이 회사·서버·계정에 따라 달라지는가?
  달라진다면 실행해야 안다". 그리고 분류가 틀릴 것에 대비해 **"모르면 모른다고 답한다"**
  안전장치를 함께 둔다(지어낸 값을 주는 것이 실패다).
- **지시문(`shared/agent_instruction.py`)에 특정 커맨드를 절대 적지 않는다**(사용자 지시, #145).
  커맨드는 두 곳에서 온다: 콘솔 등록분(툴로 노출) · 모델이 아는 표준 리눅스 명령.
  지시문은 **원칙**만 말한다("셸을 거치지 않는다", "물으면 실행해서 답한다").
  적기 시작하면 하나하나 다 적어야 하고, 시스템이 바뀌면 지시문이 거짓말을 한다 —
  `SSH_PRIVDROP`을 바꾼 것만으로 "항상 홈에서 시작한다"가 거짓이 된 것이 #144다.
  회귀 테스트 `test_instruction_names_no_specific_commands`가 막는다.
- **신뢰 경계에는 인증이 있어야 한다**(#139). 그 헤더를 그대로 믿기 때문에, 인증이 없으면
  같은 망의 누구나 헤더만 바꿔 남의 계정으로 실행할 수 있다.
  · agent-server ↔ MCP: `mcp_shared_secret`(db-init이 무작위로 심고 양쪽이 같은 DB에서 읽는다).
    맞지 않으면 MCP가 401. 관리자가 손댈 값이 아니다.
  · Open WebUI → agent-server: `agent_api_key`(콘솔). Open WebUI 연결(Connections)의 API 키와
    같게 넣으면 `/v1/*`에 인증이 걸린다. **비우면 인증이 없고**, 기동 로그에 경고가 찍힌다.
  · 내부 전용 포트(postgres·MCP 4개)는 `127.0.0.1`에만 연다. 서버에서 curl 진단은 그대로 된다.

## 4. MCP 역할 분담 (사용자 결정 사항)

- **Execution MCP**(구 Command MCP + System MCP를 #111에서 통합): 커맨드 실행 전담. **두 갈래**다
  (#128에서 코드 내장 커맨드 7개를 삭제 — 전부 LLM이 아는 표준 리눅스 명령이었다).
  · **등록 커맨드**(`execution_commands`) — 콘솔에서 `head -n {lines} {path}`처럼 등록.
    자리표시자가 **타입·설명 붙은 파라미터**로 LLM에 노출된다. 전부 편집·삭제·on/off 가능.
    자유 인자는 항상 허용(에이전트가 판단).
    인자 설명·선택지는 `Annotated[…, Field(description=…)]`/`Literal`로 스키마에 실린다(#140).
    선택지는 **`값: 설명`**(`-j: JSON 형식으로 반환`)으로 적는다 — 값만 뽑는 규칙은
    `execution_exec.choice_value` 한 곳뿐이고 `cast_arg`가 같은 것을 쓴다(어긋나면 전부 거부).
    콘솔에서 **엑셀 양식 받기 / 현재 등록분 내보내기**로 일괄 등록·수정한다(인자 포함, #140).
  · **`run_command`** — 미등록 커맨드(표준 리눅스 명령, 매뉴얼/VOC에서 찾은 것).
    차단 목록을 **모든 토큰에** 엄격 적용(`mpirun ... rm -rf /` 우회 차단).
  RAG 검색은 쓰지 않는다 — 툴 목록을 보고 에이전트가 고른다(#105).
  **등록 내용을 고치면 execution-mcp 재시작 필요**(활성/역할만 바꾸면 즉시 반영).
  실행 결과에 `duration_ms`·`connection_reused`가 실려 오고 진행 표시 줄에 초가 보인다 —
  "느리다"는 추측하지 말고 그 숫자와 `scripts/bench-exec.sh`로 가른다(#128).
- **Chart MCP**: 추이/비교를 SVG로 그린다(#110). 실행도 DB 조회도 하지 않는다.
  짧은 표시자(`chart://<id>`)만 돌려주고 **agent-server가 답변을 내보낼 때 data URI로 치환**한다
  (#114). 그래서 이미지용 설정·포트가 없다. 이력에는 표시자가 남는다(프롬프트 예산).
- Manual MCP / VOC MCP: 하이브리드 RAG 검색(읽기 전용). 매뉴얼은 **발행(published)** 해야 검색됨.

## 5. 반영 제약 (자주 놓치는 것)

| 바꾼 것 | 반영 방법 |
|---|---|
| 새 설정 키 / 마이그레이션 | `db-init` 재실행 필수 |
| **등록 커맨드 추가·수정·인자·`host_mode`·설명** | `execution-mcp` 재시작(툴 목록 재구성). 엑셀 일괄 등록도 같다 |
| `enabled` / `required_roles` | 실시간 반영(재시작 불필요) |
| `agent_system_instruction` | non-force 시드라 **기존 DB에 자동 반영 안 됨** → 콘솔 설정 탭 "지시문을 최신 기본값으로 되돌리기" 버튼 → agent-server 재시작. 원문은 `shared/agent_instruction.py` 한 곳뿐(NEXT-STEPS에 전문을 붙이지 말 것) |
| `hot_reload=false` 설정값 | 해당 서비스 재시작(콘솔에 "지금 재시작" 버튼 있음) |
| requirements 변경 | 이미지 재빌드(재시작만으로는 pip 패키지가 안 깔림) |

재시작 커맨드([서버] `cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant`):
`docker compose -f docker-compose.dev.yml restart <agent-server|execution-mcp|chart-mcp|admin-console>`
· 전부 한 번에는 `bash scripts/restart-mounted.sh` · 콘솔 재시작 버튼은
agent-server/manual-mcp/execution-mcp/voc-mcp/chart-mcp 5개만 지원(admin-console은 CLI로만).

## 6. 이미 규명된 것 — 다시 추측하지 말 것

- Open WebUI에 모델이 안 보이는 문제: 관리자 패널 → 설정 → **연결(Connections)** 에
  `http://agent-server:8000/v1` 등록 + **모델 공개범위(Visibility)를 Public** 으로.
  (콘솔의 `openwebui_admin_api_key`는 "기본 모델 동기화" 버튼 전용으로, 이것과 무관.)
- 슈퍼 admin은 Open WebUI UI에서 본인 이메일 변경 불가(업스트림 제약). DB 직접 수정 시
  `sqlite3` CLI가 이미지에 없으므로 `python3 -c "import sqlite3..."` 사용.
- MCP 421 Misdirected Request: `FastMCP(..., host="0.0.0.0")` 누락이 원인(해결됨).
- 스트리밍 미동작: ADK `RunConfig(streaming_mode=SSE)` 누락이 원인(해결됨).
