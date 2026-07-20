# AI Infra Assistant

사내 폐쇄망에서 동작하는 RAG + 툴 기반 운영 어시스턴트. Open WebUI(사용자) 뒤에서
Google ADK 에이전트가 4개의 MCP 서버(매뉴얼 RAG, 커맨드 카탈로그, VOC 이력, 화이트리스트
시스템 실행)를 오케스트레이션하고, 관리자 콘솔에서 데이터와 설정을 관리한다.

## 아키텍처

```
사용자 → Open WebUI ──(OpenAI 호환 API)──► Agent Server (FastAPI + Google ADK)
                                              │  LiteLLM → 사내 vLLM(LLM)
                                              │  MCP Client
              ┌───────────────┬──────────────┼───────────────┐
              ▼               ▼               ▼               ▼
          Manual MCP     Command MCP       VOC MCP        System MCP
          (하이브리드    (카탈로그검색+    (하이브리드    (리눅스 read-only
           RAG 검색)      본인 job 실행)    이력 검색)      ssh 원격, 본인권한)
              │               │               │               │
          manual_db       command_db       voc_db          system_db      ← MCP별 분리된 DB
              ▲
              │  임베딩(사내 vLLM)
   관리자 콘솔(FastAPI + React) ── platform_config DB(중앙 설정) ── 모든 서비스가 참조
   Langfuse ◄── OpenTelemetry ── Agent Server
```

세 개의 웹이 뜬다: **Open WebUI**(사용자 질의응답), **관리자 콘솔**(데이터·설정 관리),
**Langfuse**(트레이싱·모니터링).

## 구성

```
.
├─ agent_server/      # FastAPI + ADK, OpenAI 호환 엔드포인트 (Open WebUI가 붙는 곳)
├─ mcp_servers/
│  ├─ manual_mcp/      # 매뉴얼 하이브리드 RAG 검색
│  ├─ command_mcp/     # 커맨드 카탈로그 검색 + 본인 스케줄러 job 실행(user_scoped)
│  ├─ voc_mcp/         # VOC 이력 하이브리드 검색
│  └─ system_mcp/      # 리눅스 read-only 명령을 호출자 권한으로 실행 (whitelist.py) + 감사로그
├─ admin_console/
│  ├─ backend/         # 업로드·파싱·발행·버전관리·화이트리스트 토글·설정·로그 API
│  └─ frontend/        # 단일 HTML + React(UMD, 폐쇄망 vendor) 운영 콘솔
├─ shared/
│  ├─ config_store.py  # 중앙 설정(platform_config) 접근
│  ├─ db.py            # MCP별 DB 커넥션 풀 + vLLM 임베딩 클라이언트
│  └─ init-db/         # DB별 스키마 (Postgres 최초 기동 시 자동 실행)
├─ dev/                # 로컬 확인용 (mock vLLM). dev/README.md 참고
├─ docker-compose.yml       # 프로덕션
└─ docker-compose.dev.yml   # 개발/데모
```

## 핵심 설계

- **중앙 설정(platform_config DB)**: vLLM 주소, 각 MCP DB DSN, MCP 엔드포인트, 에이전트
  시스템 지시문을 한 곳에서 관리한다. 코드/`.env`를 여러 군데 고칠 필요 없이 **관리자 콘솔의
  설정 탭**에서 바꾼다. vLLM 주소 같은 값은 즉시 반영되고, DB DSN처럼 연결을 맺어두는 값은
  해당 서비스 재시작 후 반영된다(콘솔에 표시됨).
- **MCP별 DB 분리**: `manual_db`, `voc_db`, `command_db`, `system_db`로 물리적으로 분리.
  스키마 진화·백업·권한을 독립적으로 가져갈 수 있고, 필요하면 각 DSN을 서로 다른 Postgres
  서버로 바꿔도 코드 변경이 없다.
- **하이브리드 검색**: Manual/VOC/Command MCP는 벡터 검색과 전문검색(tsvector)을 RRF로 융합한다.
  의미 질문과 정확한 키워드/에러코드 질문 모두에 강하다. 커맨드도 정확한 이름을 몰라도
  "무엇을 하고 싶은지" 설명형으로 물으면 의미상 가까운 커맨드를 찾는다.
- **System MCP 원격 실행 안전성**: `whitelist.py`에 등록된 read-only 명령만, 타입이 정해진
  파라미터(대상 `host` + 인자)로만 노출된다(원시 셸/플래그를 LLM에 주지 않음). 실행은 사용자가
  지정한 서버로 **ssh(root) 접속 후 `su - <user_id>`로 강등**해 수행한다(그래서 남의 권한으로 못 봄).
  - `host`는 **agent 호스트의 `/etc/hosts`에 등록된 서버만** 허용된다(미등록 이름 거부 = 접근 화이트리스트).
  - 원격 명령은 **shlex로 이중 인용**(root 셸 + 사용자 셸)하고 `user_id`는 리눅스 계정명 정규식으로
    검증하므로 `;`·`|`·`$()`·백틱 주입이 불가능하다. `find`의 `-exec/-delete` 등은 금지, 타임아웃·
    출력 상한이 걸리며 `rm` 등 파괴적 명령은 등록하지 않는다.
  - 예: "hgpu8002 서버 GPU가 이상해요" → `gpu_status(host='hgpu8002')` → /etc/hosts에서 IP 조회 →
    `ssh root@… → su - <user> → nvidia-smi` → 결과로 판단.
  - **기본 비활성(disabled)**. 켜려면 (1) 대상 서버들에 root ssh 가능한 키가 컨테이너에 마운트되고,
    (2) 대상 IP가 `/etc/hosts`에 매핑되어 있어야 한다(compose 참고). 그 뒤 관리자 콘솔 System 탭에서 켠다.
  `functools.wraps`로 원본 시그니처를 보존해 MCP input schema에 실제 파라미터가 정확히
  노출된다. 실행 가능 여부(enabled)와 필요 역할(required_roles)은 콘솔에서 재배포 없이
  편집되고 실행 시점에 실시간 반영된다(설명 오버라이드는 MCP 재시작 시 반영). 모든 실행은
  호출자(사용자 ID·대화 ID·요청 ID)와 함께 `job_logs`에 감사 기록된다.
- **사용자 범위 강제(user_scoped)**: 본인 자원만 다뤄야 하는 툴(예: 스케줄러 job 조회)은
  `user_id`를 LLM 입력 스키마에서 감추고 호출자 신원(Agent Server가 붙이는 `X-User-Id`)에서
  강제 주입한다. LLM/사용자가 다른 id를 넣어도 본인 값으로 덮어쓰며, 신뢰된 id가 없으면
  실행을 거부한다(fail-closed) — 남의 job을 조회할 수 없다.
- **장애 격리**: 리랭커·임베딩·Redis 장애가 검색 전체를 막지 않는다. 리랭커 실패는 RRF 순위로,
  임베딩 실패는 키워드 전용 검색으로 fallback한다.
- **문서 무결성**: 발행된 문서의 청크는 수정·삭제할 수 없다(백엔드·프론트 양쪽 차단).
  수정하려면 새 버전을 업로드하거나, "발행취소"로 draft로 내린 뒤 교정한다. 발행취소는
  즉시 검색 대상에서 제외되고(Manual MCP는 `status='published'`만 검색), 재발행 시 임베딩을
  유지한 채 빠르게 복귀한다.

## 빠르게 로컬에서 확인 (GPU 불필요)

실제 vLLM 없이 전체 흐름을 보려면 `dev/README.md`를 따르세요:

```bash
docker compose -f docker-compose.dev.yml up -d --build
# 사용자 웹 http://localhost:3000 · 관리자 콘솔 http://localhost:8080 (admin/admin)
```

## 프로덕션 배포 (bare-metal + docker compose)

### 1. 사전 준비

```bash
cp .env.example .env
# .env에는 부트스트랩 값만 있다: POSTGRES_PASSWORD, CONFIG_DB_DSN,
# 관리자 콘솔 크리덴셜, Langfuse 키. (vLLM/MCP/DB 상세 주소는 여기 없음 → 설정 탭에서)
```

인터넷 되는 PC에서 관리자 콘솔 프론트 vendor 파일 3개를 받아둡니다
(`admin_console/frontend/vendor/README.md`, curl 3줄).

### 2. 기동

```bash
docker compose up -d --build
```

기동 순서: `postgres`(healthy) → `db-init`(스키마 마이그레이션 + 설정 부트스트랩 완료) →
나머지 서비스. `db-init`은 매 기동 시 멱등하게 실행되며,

- DB/Redis 접속 정보를 `.env`의 `POSTGRES_PASSWORD`/`REDIS_PASSWORD`로부터 만들어 주입합니다.
  즉 비밀번호를 바꾸면 각 MCP의 DSN이 **자동으로 맞춰집니다**(SQL에 credential 하드코딩 없음).
- 버전별 마이그레이션(`shared/migrations.py`의 `MIGRATIONS`)을 적용하므로, 최초 설치뿐 아니라
  **기존 DB 업그레이드에도 스키마·신규 설정이 반영**됩니다.
- 운영자가 콘솔에서 편집한 설정값은 재기동해도 덮어쓰지 않습니다.

내부 서비스(PostgreSQL·Redis·MCP·Agent Server)는 호스트 포트를 열지 않고, 웹 3개만
`127.0.0.1`에 바인딩됩니다. 외부 공개는 reverse proxy를 통해서만 하세요.

### 3. 설정 채우기 (중요)

관리자 콘솔(http://서버:8080) → **설정 탭**에서 `CHANGE-ME`로 되어 있는 vLLM 주소들을 실제
사내 vLLM 서버 주소로 바꿉니다:
- `vllm_llm_base_url`, `vllm_llm_model` — LLM 서빙 서버
- `vllm_embed_base_url`, `vllm_embed_model` — 임베딩 서버
- `scheduler_login_host` — Command MCP가 job 조회 시 ssh할 로그인 서버(/etc/hosts 등록명, 예: login05)

vLLM 주소는 즉시 반영됩니다. (DB DSN을 바꾼 경우 해당 서비스만 `docker compose restart`)

### 4. 관리자 콘솔 사용

- **매뉴얼 탭**: docx/pptx/pdf는 업로드 시 Docling이 자동 청크화. 엑셀(xlsx)은 업로드하면
  컬럼 선택 화면이 뜨고, 내용으로 쓸 컬럼(과 제목 컬럼)을 골라 등록. 청크를 교정한 뒤
  "발행"하면 그 시점에 임베딩되어 검색 대상이 됨. 같은 제목 재업로드→새 버전, 발행 시 이전
  버전 자동 archived, archived를 다시 발행하면 롤백. "발행취소"를 누르면 즉시 검색에서 빠지고
  다시 교정·삭제할 수 있음(파일 단위 관리: 파일을 지우면 소속 청크가 함께 삭제됨).
- **VOC 탭**: 개별 등록/수정/삭제, 또는 `question`/`answer`/`department`/`resolved` 엑셀 일괄 등록.
- **커맨드 카탈로그 탭**: Command MCP가 조회하는 커맨드 CRUD (Command MCP는 이 카탈로그 검색과
  함께 본인 스케줄러 job 조회도 담당). 개별 등록 외에 엑셀 일괄 업로드
  지원 — 업로드하면 열 매핑 화면이 뜨고 name/description/usage/category에 대응할 열을 골라
  이름 기준 upsert. 등록/갱신 시 정제·임베딩이 적용되어 의미 검색 대상이 됨.
- **System MCP 탭**: 리눅스 read-only 명령 툴의 on/off 토글, 설명·필요 역할 편집, 실행 감사로그.
  이 툴들은 기본 비활성이며 인프라 요건(위 참고)이 갖춰진 뒤 켠다. "사용자 범위" 배지 툴은
  호출자 본인 권한으로만 실행됨. 역할은 이제 Open WebUI 로그인 역할이 전달되므로
  (`ENABLE_FORWARD_USER_INFO_HEADERS`) 지정한 역할 보유자만 실행하도록 제한할 수 있음.
- **설정 탭**: 위 3번의 모든 주소/모델/지시문 관리.

### 5. Langfuse (모니터링)

http://서버:3001 접속 → 계정/프로젝트 생성 → API 키 발급 → `.env`의 `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY`에 넣고 `docker compose up -d agent-server` 재기동. 이후 모든 LLM 호출과
MCP 툴 호출이 트레이스로 쌓입니다. (키가 없으면 에이전트는 트레이싱 없이 정상 동작)

## 상위 agent 연동 + 사용자 장기 메모리

Open WebUI 외에, 상위 통합 agent(예: VOC agent)가 AI-Infra 질문을 **API로 위임**할 수 있다.

```
POST /v1/agent/query            # 내부망 전용(인증 없음)
{ "user_id": "hong.gildong", "message": "...", "conversation_id": "voc-123",
  "source": "voc-agent", "roles": ["user"], "use_memory": true, "stream": false }
→ { "answer": "...", "conversation_id": "voc-123", "request_id": "..." }
```

- **단일 user_id 메모리**: user_id는 이메일이면 `@` 앞부분으로 정규화된다. 채널(Open WebUI/
  VOC)이 달라도 같은 user_id면 **하나의 장기 메모리를 공유**한다.
- **동작**: 요청 시 (1) 해당 대화의 최근 N턴 + (2) user_id의 장기기억을 질문 임베딩으로 의미검색해
  시스템 지시문/대화 이력에 주입한다. 응답 후 대화 턴을 저장하고, 턴이 임계치(`memory_summarize_every`)만큼
  쌓이면 오래된 대화를 vLLM으로 **요약·증류**해 `user_memory`(장기기억)로 승격한다(백그라운드).
- **저장소**: 전용 `memory_db`(pgvector). `memory_turns`(원장)/`user_memory`(증류된 장기기억)/
  `conversation_state`(요약 진행). 파라미터는 설정 탭의 `memory_*` 키로 조절.
- **관리 API**: `GET /v1/memory/{user_id}`(조회), `POST /v1/memory/{user_id}`(수동 추가),
  `DELETE /v1/memory/{user_id}`(개별 `?memory_id=` 또는 전체 삭제=잊힐 권리).

### 통합 VOC agent 연동 (`POST /v1/voc/query`)

통합 VOC agent가 AI-Infra 관련 VOC를 위임하는 전용 엔드포인트(가이드 계약).
```
입력:  { "voc_info": { voc_id, voc_title, system{}, sub_system{}, requester{ user_id, ... },
                       voc_content{ text, raw_text }, ... },
         "output_option": "html" | "markdown" }   # 답변 형식 강제
출력:  { "success": true, "answer": { "content": "<h2>…" } }   # 필수: success, answer.content
        (실패 시 { "success": false, "answer": null })
```
- `requester.user_id`로 **장기 메모리를 공유**(Open WebUI/agent와 동일 저장소), `voc_id`를
  대화 스레드(conversation_id)로 사용.
- `output_option`에 따라 답변을 **HTML/마크다운으로 강제** 출력.
- `stream:true`면 SSE로 `{"delta":…}`를 흘리고 마지막에 완성 envelope + `[DONE]`.
- `answer.similar_voc`: **Service Hub MCP를 직접 호출**해 후처리로 채운다(에이전트 응답과 병렬).
  현재 VOC의 시스템명으로 `rag_filtered_search`(없으면 `rag_keyword_search`)를 호출해 상위 N개를
  `{voc_id?, title, system?, reason}`로 매핑한다. 설정 키 `service_hub_mcp_url`가 비어 있으면
  (방화벽 미개통 등) **조용히 생략**하고 나머지 답변은 정상 반환한다(`voc_similar_top_k`로 개수 조절).
  주의: rag 검색 응답에 `voc_id`/`system`이 없을 수 있어, 있으면 채우고 없으면 생략한다.
- `evaluation`(선택)은 추후.
- 주의: 이 엔드포인트들은 인증이 없으므로 **agent-server를 내부망에서만** 접근 가능하게 둔다
  (compose 기본은 호스트 미노출). 외부 노출 시 reverse proxy에서 접근 제한 필요.

## 에이전트가 보는 MCP 목적/기능을 바꾸려면

에이전트의 시스템 지시문(각 MCP를 언제 쓰라는 지침 포함)은 관리자 콘솔 **설정 탭 →
`agent_system_instruction`**에서 바꾼 뒤 agent-server를 재시작하면 됩니다. 개별 MCP 툴의
설명(에이전트가 툴 선택에 참고하는 docstring)은 각 `mcp_servers/*/server.py`의 툴 함수
docstring이며, 이는 코드 배포로 반영됩니다.

## 폐쇄망 참고 (Docling)

`admin_console/backend/parser.py`의 Docling은 최초 실행 시 레이아웃 모델을 내려받습니다.
인터넷 되는 환경에서 모델 캐시를 미리 받아 `admin_console` 이미지에 포함해야 합니다:

```bash
pip install docling
python -c "from docling.document_converter import DocumentConverter; DocumentConverter()"
# ~/.cache/docling 를 이미지에 COPY (admin_console/Dockerfile에 COPY 한 줄 추가)
```


## 테스트

```bash
pip install pytest
DISABLE_DOCLING=1 CONFIG_DB_DSN=postgresql://x:x@localhost/x \
  PYTHONPATH=shared:admin_console/backend python -m pytest tests/ -v
```

리뷰에서 지적된 버그를 고정하는 회귀 테스트가 들어 있습니다: 정제가 `<user>`/`<namespace>`
같은 인프라 placeholder를 지우지 않는지, PPT 표·그룹 도형 텍스트가 누락되지 않는지,
리랭커가 어떤 실패에도 안전하게 fallback하는지, System MCP 툴 스키마에 실제 파라미터가
노출되는지 등입니다.
