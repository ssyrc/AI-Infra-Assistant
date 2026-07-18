# DEV 환경 (로컬에서 GPU/vLLM 없이 확인)

실제 vLLM·GPU 없이 웹 3개 + 에이전트 + MCP 4개 흐름을 로컬에서 확인하기 위한 구성입니다.
mock vLLM(`dev/mock_vllm.py`)이 임베딩·LLM 응답을 흉내냅니다.

## 실행

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

기동되는 것:

| 웹/서비스 | 주소 | 비고 |
|---|---|---|
| Open WebUI (사용자) | http://localhost:3000 | 인증 없음(dev), agent-server에 자동 연결 |
| 관리자 콘솔 | http://localhost:8080 | ID/PW: `admin` / `admin` |
| Agent Server | http://localhost:8000/v1 | OpenAI 호환 |
| mock vLLM | http://localhost:8100/v1 | 가짜 임베딩/LLM |
| Manual/Command/VOC/System MCP | 8001~8004 | |
| PostgreSQL | localhost:5432 | user/pw: `agent`/`devpass` |

Langfuse는 dev 구성에서 제외했습니다(무겁고 키 발급 과정 필요). agent-server는
Langfuse 키가 없으면 트레이싱을 자동으로 비활성화하고 정상 동작합니다.

## 확인 시나리오

1. **관리자 콘솔에서 데이터 넣기** (http://localhost:8080)
   - VOC 이력 탭: 질문/답변을 직접 등록하거나 엑셀(`question`,`answer` 컬럼) 업로드
   - 커맨드 카탈로그 탭: 커맨드 몇 개 등록
   - 매뉴얼 탭: **엑셀 파일**을 올리면 컬럼 선택 화면이 뜸 → 내용 컬럼 선택 → 등록 → 발행
     (docx/pptx/pdf는 dev에서 파싱 비활성 - 프로덕션에서 확인)
   - 설정 탭: mock으로 세팅된 vLLM 주소 등을 확인/변경

2. **사용자 웹에서 질문** (http://localhost:3000)
   - 메시지를 보내면 agent가 mock LLM으로 응답. mock은 요청을 에코하지만,
     실제로 MCP 툴 호출 흐름(에이전트→MCP→DB 검색)은 그대로 동작함을 로그로 확인 가능.

3. **검색 동작 확인**
   - mock 임베딩은 의미 유사도가 없으므로(해시 기반) 벡터 순위는 의미가 없지만,
     하이브리드 검색의 키워드(전문검색) 경로 덕분에 정확히 일치하는 단어가 있으면 검색됩니다.
   - 즉 "DB 저장 → 검색 → 반환" 파이프라인 자체는 dev에서 검증 가능하고,
     의미 검색 품질만 실제 임베딩 서버가 붙는 프로덕션에서 최종 확인하면 됩니다.

## 정리

```bash
docker compose -f docker-compose.dev.yml down -v   # -v: DB 볼륨까지 삭제
```

## 프로덕션과의 차이

| 항목 | dev | 프로덕션(docker-compose.yml) |
|---|---|---|
| vLLM | mock (dev/mock_vllm.py) | 사내 실제 vLLM 서버 (설정 탭에서 주소 지정) |
| 문서 파싱(docx/pptx/pdf) | 비활성(DISABLE_DOCLING) | Docling |
| Langfuse | 없음 | 포함 |
| 인증 | Open WebUI 무인증, admin/admin | 실제 크리덴셜 + 리버스 프록시 |
