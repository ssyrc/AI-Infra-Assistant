# AI Infra Assistant

사내 폐쇄망에서 동작하는 RAG + 툴 기반 운영 어시스턴트. Open WebUI(사용자)나 상위 통합 agent
뒤에서 Google ADK 에이전트가 4개의 MCP 서버를 오케스트레이션하고, 관리자 콘솔에서 데이터와
설정을 관리한다.

---

## 아키텍처

```
사용자 ─ Open WebUI ─┐
                     ├─(OpenAI 호환 / VOC API)─► Agent Server (FastAPI + Google ADK)
상위 VOC agent ──────┘                            │  LiteLLM → 사내 vLLM(LLM)
                                                  │  MCP Client (요청별 호출자 헤더 주입)
              ┌───────────────┬──────────────┬────┴──────────┐
              ▼               ▼               ▼               ▼
          Manual MCP     Command MCP       VOC MCP        System MCP
          (하이브리드    (카탈로그검색+    (하이브리드    (리눅스 read-only
           RAG 검색)      본인 job 실행)    이력 검색)      ssh 원격, 본인권한)
              │               │               │               │
          manual_db       command_db       voc_db          system_db   ← MCP별 분리 DB
                                                  │
   memory_db(사용자 장기기억) ◄── Agent Server ──┤  임베딩(사내 vLLM)
   platform_config(중앙 설정) ◄── 모든 서비스가 참조
   Langfuse ◄── OpenTelemetry ── Agent Server (user_id/session 태깅)
```

세 개의 웹: **Open WebUI**(사용자), **관리자 콘솔**(데이터·설정), **Langfuse**(트레이싱).

### 구성

```
agent_server/    # FastAPI + ADK. OpenAI 호환(/v1/chat/completions) + agent/VOC API + 메모리 API
mcp_servers/
  manual_mcp/    # 매뉴얼 하이브리드 RAG 검색
  command_mcp/   # 커맨드 카탈로그 검색 + 본인 스케줄러 job 실행(user_scoped, ssh)
  voc_mcp/       # VOC 이력 하이브리드 검색
  system_mcp/    # 리눅스 read-only 명령을 호출자 권한으로 ssh 실행(whitelist.py) + 감사로그
admin_console/   # backend(업로드·파싱·발행·버전·설정·로그 API) + frontend(단일 HTML React)
shared/          # config_store·db·migrations·memory_store·ssh_exec·mcp_caller·service_hub·init-db
dev/             # 로컬 확인용 mock vLLM (dev/README.md)
docker-compose.yml       # 프로덕션
docker-compose.dev.yml   # 개발/데모(mock)
docs/TESTING.md          # 서버 테스트 step-by-step
```

### 핵심 설계 원칙

- **중앙 설정(platform_config DB)**: vLLM 주소·MCP DSN·엔드포인트·에이전트 지시문을 한 곳에서
  관리한다. 코드/`.env`를 여러 군데 고칠 필요 없이 **관리자 콘솔 설정 탭**에서 바꾼다. 주소류는
  즉시 반영, DB DSN처럼 연결을 맺는 값은 해당 서비스 재시작 후 반영(콘솔에 표시).
- **MCP별 DB 분리**: `manual_db`/`voc_db`/`command_db`/`system_db`를 물리적으로 분리. 스키마·백업·
  권한을 독립적으로 가져가고, DSN만 바꿔 다른 Postgres로 옮겨도 코드 변경이 없다.
- **하이브리드 검색**: Manual/VOC/Command는 벡터 + 전문검색(tsvector)을 RRF로 융합 후 리랭크.
  의미 질문과 정확한 키워드/에러코드 질문 모두에 강하다.
- **user_scoped 강제**: 본인 자원 툴(스케줄러 job, 리눅스 명령)은 `user_id`를 LLM 스키마에서
  감추고 호출자 신원(`X-User-Id`)에서 강제 주입한다. 다른 id를 넣어도 본인으로 덮어쓰며, 신뢰된
  id가 없으면 실행 거부(fail-closed) — 남의 자원에 접근할 수 없다.
- **System MCP 원격 실행 안전성**: 등록된 read-only 명령만 타입 지정 파라미터(대상 `host` + 인자)로
  노출. 실행은 `host`(agent 호스트 `/etc/hosts`에 등록된 서버만)로 **ssh(root) → `su - <user>`**.
  원격 명령은 shlex 이중 인용, `user_id`는 계정명 정규식 검증으로 셸 주입 불가. `find -exec/-delete`
  금지, 타임아웃·출력 상한, `rm` 미등록. enabled·required_roles는 콘솔에서 실시간 편집, 모든 실행은
  `job_logs`에 감사. **기본 비활성**(ssh 키·/etc/hosts 준비 후 콘솔에서 켬).
- **사용자 장기 메모리(memory_db)**: Open WebUI·상위 agent 모두 **단일 user_id 키로 기억을 공유**.
  요청 시 최근 대화 턴 + 의미검색한 장기기억을 주입하고, 응답 후 저장하며 임계치마다 vLLM으로
  **요약·증류**한다. Langfuse는 trace에 user_id/session을 태깅해 사용자별 관찰.
- **장애 격리**: 리랭커·임베딩·Redis 장애가 검색을 막지 않는다(리랭커→RRF, 임베딩→키워드 fallback).
- **문서 무결성**: 발행된 청크는 수정·삭제 불가. 새 버전 업로드 또는 "발행취소"(즉시 검색 제외,
  재발행 시 임베딩 유지) 로만 교정한다.

---

## 실행 가이드

### 빠른 확인 (GPU/LLM 불필요)

```bash
docker compose -f docker-compose.dev.yml up -d --build
# 사용자 웹 :3000 · 관리자 콘솔 :8080 (admin/admin) · Agent :8000
```
mock vLLM으로 전체 흐름을 확인한다. 상세 검증 절차는 **`docs/TESTING.md`**.

### 프로덕션

```bash
cp .env.example .env      # POSTGRES/ADMIN/REDIS 비밀번호, vLLM 주소(VLLM_*), Langfuse 키, SSH_KEY_PATH
docker compose up -d --build
```
- 기동 순서: `postgres`(healthy) → `db-init`(마이그레이션 + 설정 시드, 멱등) → 나머지.
  비밀번호를 바꾸면 각 MCP DSN이 자동으로 맞춰진다(credential 하드코딩 없음).
- 내부 서비스는 호스트 포트를 열지 않고 웹 3개만 `127.0.0.1` 바인딩 → 외부는 reverse proxy로만.
- 기동 후 **관리자 콘솔 설정 탭**에서 `vllm_*`/`rerank_*`/`scheduler_login_host` 등 주소 확정.
- 폐쇄망 준비물: 프론트 vendor 3파일(`admin_console/frontend/vendor/README.md`), Docling 모델 캐시
  (`admin_console` 이미지에 포함), Langfuse 키(선택 — 없으면 트레이싱만 비활성).

---

## 데이터 전처리 방법

관리자 콘솔에서 업로드하면 파일 종류별로 자동 정제·청크화된다. **공통 정제**: 실제 HTML 태그·엔티티
제거, 제어문자·과도한 공백 정리(옵션 토글 가능), 코드블록(``` `code` ```)과 `<user>`·`<namespace>`
같은 인프라 placeholder는 보존.

| 종류 | 처리(관리자 콘솔 탭) |
|---|---|
| `txt` / `md` | 문단/마크다운 헤더 기준 분할 (매뉴얼 탭) |
| `pptx` | 슬라이드 단위 청크. 그룹 도형·표 텍스트 포함, 발표자 노트 옵션 (매뉴얼 탭) |
| `docx` / `pdf` | Docling으로 마크다운 변환 → 헤더 기준 섹션화 (매뉴얼 탭, 프로덕션) |
| `xlsx` (매뉴얼) | 열 선택 화면 → 고른 열을 `열이름: 값`으로 행 단위 청크 |
| `xlsx` (VOC) | `question`/`answer`/`department`/`resolved` 컬럼 일괄 등록 |
| `xlsx` (커맨드) | 열 매핑(name/description/usage/category) → 이름 기준 upsert |

- **발행/버전**: 업로드→미리보기→(청크 교정)→**발행** 시점에 임베딩되어 검색 대상이 됨. 같은 제목
  재업로드=새 버전, 발행 시 이전 버전 자동 archived, archived 재발행=롤백. "발행취소"는 즉시 검색
  제외 + 파일 단위 삭제/교정 가능(파일 삭제 시 소속 청크 CASCADE).
- **커맨드/VOC**: 등록·수정 시 정제·임베딩이 적용되어 즉시 의미검색 대상이 됨.
- 에이전트가 툴을 언제 쓰는지(지시문)는 설정 탭 `agent_system_instruction`에서, 개별 툴 설명은 각
  `mcp_servers/*/server.py` docstring에서 관리.

---

## API 와 응답 예시

### 사용자 (OpenAI 호환)
`POST /v1/chat/completions` — Open WebUI가 사용. 표준 OpenAI 요청/응답, `stream` 지원.

### 상위 agent 위임
```jsonc
// POST /v1/agent/query   (내부망 전용, 인증 없음)
{ "user_id":"hong.gildong", "message":"…", "conversation_id":"voc-123",
  "source":"voc-agent", "use_memory":true, "stream":false }
// → { "answer":"…", "conversation_id":"voc-123", "request_id":"agentq-…" }
```

### 통합 VOC agent 연동
```jsonc
// POST /v1/voc/query
// 입력
{ "voc_info": { "voc_id":"2026041616503589073", "voc_title":"버튼 동작 오류",
                "system": { "id":"SSTM01420", "name":"Service Hub" },
                "requester": { "user_id":"sg.chon", "user_name":"전성균" },
                "voc_content": { "text":"등록/답변/평가 버튼이 동작하지 않습니다",
                                 "raw_text":"<div>…원본 HTML…</div>" } },
  "output_option": "html" }              // "html" | "markdown"
// 출력 (필수: success, answer.content)
{ "success": true,
  "answer": {
    "content": "<h2>문제 분석</h2><p>…</p>",
    "similar_voc": [                      // 선택: Service Hub MCP 연동 시 채워짐
      { "voc_id":"2025…", "title":"유사 VOC", "system":"물류", "reason":"…" }
    ] } }
// 실패: { "success": false, "answer": null }
```
- `requester.user_id`로 장기 메모리 공유, `voc_id`를 대화 스레드로 사용. `output_option`에 따라 답변을
  HTML/마크다운으로 강제. `conversation_id`가 없으면 `auto-<user_id>-<UTC일자>`로 자동 부여.
- `stream:true`면 SSE로 `{"delta":…}` 후 완성 envelope + `[DONE]`.
- `similar_voc`는 **Service Hub MCP를 직접 호출**해 후처리로 채운다(시스템명으로 `rag_filtered_search`).
  `service_hub_mcp_url` 미설정 시 조용히 생략하고 답변은 정상 반환.

### 사용자 장기 메모리 관리
`GET /v1/memory/{user_id}`(조회) · `POST /v1/memory/{user_id}`(추가) · `DELETE /v1/memory/{user_id}`
(개별 `?memory_id=` 또는 전체=잊힐 권리).

> 조회/감사는 **Langfuse**(사용자별 trace), 기억의 실제 저장·주입·요약은 **memory_db**가 담당한다.
> 이 API들은 인증이 없으므로 agent-server를 내부망에서만 접근 가능하게 둔다.

---

## 외부 Service Hub MCP 연동 (similar_voc)

Service Hub MCP는 **외부에서 제공하는 원격 MCP 서버**(streamable-http)로, VOC/서비스허브 지식에 대한
RAG 검색 툴을 노출한다. 우리 에이전트의 4개 MCP와 달리 **에이전트 툴로 붙이지 않고**,
`/v1/voc/query`에서 **직접 호출해 `similar_voc`를 후처리로 채운다**(결정적 출력, 에이전트 응답과 병렬).

| 툴 | 용도 |
|---|---|
| `rag_keyword_search` | 키워드 RAG 검색 (`query`, `num_result_doc`) |
| `rag_filtered_search` | 메타데이터 필터 검색 (`query` + `system_name`/`sub_system_name`/`division_name`/…) |
| `get_voc_data_by_id` | VOC ID로 상세 조회 |
| `voc_statistics` | VOC 통계 (시스템별 건수 등) |
| `get_metadata_hierarchy` | 시스템 메타데이터 계층 |

- **동작**: 현재 VOC의 시스템명이 있으면 `rag_filtered_search`(같은 시스템으로 좁힘), 없으면
  `rag_keyword_search`를 호출 → 상위 N개를 `{voc_id?, title, system?, reason}`로 매핑.
- **방어적 매핑**: rag 검색 응답은 `{title, content, score}`만 주고 `voc_id`/`system`이 없을 수 있다.
  있으면 채우고(여러 키 시도), `system`은 필터로 쓴 `system_name`을 fallback, `reason`은 `content`
  스니펫으로 만든다. (실서버가 더 많은 필드를 주면 자동 반영)
- **설정**: `service_hub_mcp_url`(비우면 similar_voc 생략), `voc_similar_top_k`(개수, 0이면 비활성).
  인증/헤더 불필요. `shared/service_hub.py` 담당.
- **장애 격리**: URL 미설정(방화벽 미개통)·연결 실패·타임아웃이면 **조용히 빈 리스트**를 반환하고
  본문 답변은 정상 반환한다(요청 실패 아님).

> URL: `http://innodev--etl-prod.…/mcp` (방화벽 개통 후 설정 탭에 입력). MCP는 self-describing이라
> URL만 있으면 되고, 방화벽 개통 후 rag 응답에 `voc_id`가 실제로 오는지 1건으로 확인해 매핑을 확정한다.

---

## 테스트

서버 스모크 테스트는 **`docs/TESTING.md`**. 단위 회귀 테스트:

```bash
pip install pytest
DISABLE_DOCLING=1 CONFIG_DB_DSN=postgresql://x:x@localhost/x \
  PYTHONPATH=shared:admin_console/backend python -m pytest tests/ -v
```
