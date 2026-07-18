# Agent Platform - Agent Server + 4 MCP + 관리자 콘솔 + Open WebUI + Langfuse

## 구성

```
agent-platform/
  agent_server/        # FastAPI + ADK, OpenAI 호환 엔드포인트 (Open WebUI가 붙는 곳)
  mcp_servers/
    manual_mcp/         # 매뉴얼 RAG 검색
    command_mcp/        # 커맨드 카탈로그 조회
    voc_mcp/             # VOC 이력 검색
    system_mcp/          # 화이트리스트 실행 (whitelist.py)
  admin_console/
    backend/             # FastAPI: 업로드/파싱/발행/버전관리/화이트리스트 토글/로그 조회
    frontend/             # 단일 HTML + React(UMD, 폐쇄망 vendor) 운영 콘솔
  shared/
    db.py               # 공용 PG/pgvector + vLLM 임베딩 클라이언트
    schema.sql          # 테이블/인덱스 정의
  docker-compose.yml
  .env.example
```

## 1. 사전 준비

```bash
cp .env.example .env
# .env 열어서 아래 값 채우기:
#   VLLM_LLM_BASE_URL, VLLM_EMBED_BASE_URL  -> 사내 vLLM 서버 주소
#   SCHEDULER_API_BASE_URL                  -> s2 스케줄러 API 주소
#   POSTGRES_PASSWORD, ADMIN_USER/PASSWORD  -> 원하는 값
```

인터넷이 되는 PC에서 관리자 콘솔 프론트엔드 vendor 파일을 받아둡니다
(`admin_console/frontend/vendor/README.md` 참고, curl 3줄).

## 2. 기동

```bash
docker compose up -d --build
```

- `postgres` 기동 시 `shared/schema.sql`이 자동 실행되어 테이블이 생성됩니다.
- `manual-mcp`, `command-mcp`, `voc-mcp`, `system-mcp`가 각각 8001~8004 포트로 뜹니다.
- `agent-server`가 8000 포트에서 위 4개 MCP를 연결한 ADK 에이전트를 OpenAI 호환
  `/v1/chat/completions`로 노출합니다.
- `admin-console`이 8080 포트에서 뜹니다 (아래 3번 참고).

## 3. 관리자 콘솔 (매뉴얼/VOC/커맨드/화이트리스트 관리)

`http://<서버IP>:8080` 접속 → HTTP Basic 인증(`.env`의 `ADMIN_USER`/`ADMIN_PASSWORD`) 입력.

**매뉴얼 탭**
1. 제목(버전 그룹핑 키)과 xlsx/docx/pptx/pdf 파일을 업로드 → Docling이 구조를 보존한
   청크로 자동 분해 (`admin_console/backend/parser.py`)
2. 목록에서 문서 클릭 → 청크별로 텍스트를 직접 교정/삭제 가능 (draft 상태에서 자유롭게 수정)
3. "발행" 클릭 → 그 시점에만 vLLM 임베딩 서버로 임베딩 계산 후 `manual_files.status='published'`로
   전환, Manual MCP가 즉시 새 내용으로 검색
4. 같은 제목으로 재업로드하면 새 버전(draft)이 생기고, 발행하면 이전 published 버전은
   자동으로 archived로 내려감 → 문서 상세 화면에서 과거 archived 버전을 다시 "발행"하면
   **롤백**과 동일하게 동작 (이미 임베딩되어 있어 재계산 없이 즉시 반영)

**VOC 탭**: 개별 등록/수정/삭제, 또는 `question`/`answer`/`department`/`resolved` 컬럼을
가진 엑셀을 올려 일괄 등록.

**커맨드 카탈로그 탭**: Command MCP가 조회하는 이름/설명/사용법 CRUD.

**System MCP 탭**: `whitelist.py`에 코드로 등록된 함수들의 실행 가능 여부를 토글
(재배포 없이 즉시 반영, System MCP가 호출 시점에 DB를 확인함) + 실행 감사로그 조회.

## 4. Langfuse 연동 (모니터링 웹)

1. `http://<서버IP>:3001` 접속 → 최초 관리자 계정 생성 → Organization/Project 생성
2. Project Settings에서 API Key(public/secret) 발급
3. `.env`의 `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`에 채워넣기
4. `docker compose up -d --build agent-server` 로 재기동

이후 Open WebUI에서 질문할 때마다 Langfuse 대시보드에 트레이스(LLM 호출, 4개 MCP 툴 호출,
지연시간, 토큰 사용량)가 자동으로 쌓입니다. `GoogleADKInstrumentor().instrument()` 한 줄로
ADK의 모든 실행이 OpenTelemetry span으로 export되기 때문입니다 (`agent_server/agent.py` 상단).

## 5. Open WebUI 연동 (사용자 웹)

docker-compose에 이미 연결되어 있습니다 (`OPENAI_API_BASE_URL=http://agent-server:8000/v1`).

1. `http://<서버IP>:3000` 접속
2. 모델 목록에 `VLLM_LLM_MODEL`로 설정한 이름(예: `qwen3-32b`)이 보이면 정상 연결
3. 채팅 시작 시 Open WebUI가 매 요청마다 `/v1/chat/completions`를 호출하고,
   `agent_server/main.py`가 사용자 단위(세션)로 ADK Runner를 실행 → 4개 MCP 중
   필요한 걸 에이전트가 스스로 골라서 호출

별도 인스턴스에 이미 Open WebUI가 떠 있다면, 이 compose의 `open-webui` 서비스는
빼고 기존 Open WebUI의 관리자 설정 > Connections에서
`http://<agent-server-host>:8000/v1`을 커스텀 OpenAI 엔드포인트로 추가하면 됩니다.

## 6. System MCP 화이트리스트 확장

`mcp_servers/system_mcp/whitelist.py`에 함수를 추가하고 `WHITELIST` 딕셔너리에
등록 후 재배포합니다. 임의 커맨드 실행 경로는 없으며, 등록된 함수만 MCP 툴로
노출되고 모든 실행은 `job_logs` 테이블에 감사 기록됩니다.

## 7. 로컬 개발 시 개별 MCP 서버만 실행

```bash
cd agent-platform
pip install -r mcp_servers/requirements.txt
export PG_DSN=postgresql://agent:changeme@localhost:5432/agent_platform
export VLLM_EMBED_BASE_URL=http://<embed-server>:8010/v1
python mcp_servers/manual_mcp/server.py
```

## 8. 폐쇄망 참고사항 (Docling)

`admin_console/backend/parser.py`는 Docling으로 문서를 파싱합니다. Docling은 최초 실행 시
레이아웃/OCR 모델을 인터넷에서 내려받으므로, 인터넷이 되는 환경에서 아래처럼 모델을
미리 받아 이미지 빌드에 포함시켜야 합니다.

```bash
pip install docling
python -c "from docling.document_converter import DocumentConverter; DocumentConverter()"
# 기본적으로 ~/.cache/docling 에 모델이 캐시됨 -> 이 디렉토리를 admin_console 이미지에 COPY
```

`admin_console/Dockerfile`에 `COPY docling_cache/ /root/.cache/docling/` 한 줄을 추가하는
방식으로 반영하면 됩니다.

