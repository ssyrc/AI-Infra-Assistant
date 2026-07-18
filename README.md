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
          (하이브리드    (커맨드 카탈로그) (하이브리드    (화이트리스트
           RAG 검색)                        이력 검색)      실행만)
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
│  ├─ command_mcp/     # 커맨드 카탈로그 조회
│  ├─ voc_mcp/         # VOC 이력 하이브리드 검색
│  └─ system_mcp/      # 화이트리스트 실행 (whitelist.py) + 감사로그
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
- **하이브리드 검색**: Manual/VOC MCP는 벡터 검색과 전문검색(tsvector)을 RRF로 융합한다.
  의미 질문과 정확한 키워드/에러코드 질문 모두에 강하다.
- **System MCP 안전성**: `whitelist.py`에 등록된 함수만 노출되고 임의 셸 실행 경로가 없다.
  실행 가능 여부는 관리자 콘솔에서 재배포 없이 토글되며 모든 실행은 `job_logs`에 감사 기록된다.

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

`postgres` 최초 기동 시 `shared/init-db/*.sql`이 순서대로 실행되어 DB들(platform_config,
manual_db, voc_db, command_db, system_db, agent_sessions_db, langfuse)과 스키마, 기본
설정값이 생성됩니다.

### 3. 설정 채우기 (중요)

관리자 콘솔(http://서버:8080) → **설정 탭**에서 `CHANGE-ME`로 되어 있는 vLLM 주소들을 실제
사내 vLLM 서버 주소로 바꿉니다:
- `vllm_llm_base_url`, `vllm_llm_model` — LLM 서빙 서버
- `vllm_embed_base_url`, `vllm_embed_model` — 임베딩 서버
- `scheduler_api_base_url` — System MCP가 호출할 s2 스케줄러 API

vLLM 주소는 즉시 반영됩니다. (DB DSN을 바꾼 경우 해당 서비스만 `docker compose restart`)

### 4. 관리자 콘솔 사용

- **매뉴얼 탭**: docx/pptx/pdf는 업로드 시 Docling이 자동 청크화. 엑셀(xlsx)은 업로드하면
  컬럼 선택 화면이 뜨고, 내용으로 쓸 컬럼(과 제목 컬럼)을 골라 등록. 청크를 교정한 뒤
  "발행"하면 그 시점에 임베딩되어 검색 대상이 됨. 같은 제목 재업로드→새 버전, 발행 시 이전
  버전 자동 archived, archived를 다시 발행하면 롤백.
- **VOC 탭**: 개별 등록/수정/삭제, 또는 `question`/`answer`/`department`/`resolved` 엑셀 일괄 등록.
- **커맨드 카탈로그 탭**: Command MCP가 조회하는 커맨드 CRUD.
- **System MCP 탭**: 화이트리스트 함수 on/off 토글 + 실행 감사로그.
- **설정 탭**: 위 3번의 모든 주소/모델/지시문 관리.

### 5. Langfuse (모니터링)

http://서버:3001 접속 → 계정/프로젝트 생성 → API 키 발급 → `.env`의 `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY`에 넣고 `docker compose up -d agent-server` 재기동. 이후 모든 LLM 호출과
MCP 툴 호출이 트레이스로 쌓입니다. (키가 없으면 에이전트는 트레이싱 없이 정상 동작)

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
