# 서버 테스트 가이드

LLM/GPU 없이 **dev(mock) 구성**으로 전체 파이프라인을 먼저 검증하고, vLLM이 준비되면 **프로덕션**으로 넘어간다.

---

## 0. 사전 준비

```bash
docker --version && docker compose version
```

### 폐쇄망 이미지 레지스트리 (사내 미러)

compose는 `.env`의 레지스트리 접두사를 참조한다(비우면 공개 레지스트리). 폐쇄망은 `.env`에 사내
미러를 지정한다:

```bash
cp .env.example .env
# .env 안에서 (예시)
#   REGISTRY_DOCKERHUB=repository.samsungds.net/proxy-docker-registry-1.docker.io
#   REGISTRY_GHCR=repository.samsungds.net/proxy-docker-ghcr.io
```

dev에 필요한 이미지 4종(미리 미러에서 받아두거나 빌드 시 자동 다운로드):

```
<REGISTRY_DOCKERHUB>/pgvector/pgvector:pg16
<REGISTRY_DOCKERHUB>/postgres:16-alpine
<REGISTRY_DOCKERHUB>/python:3.12-slim        # MCP/agent/admin/mock 빌드 베이스
<REGISTRY_GHCR>/open-webui/open-webui:v0.6.5
```

> 프로덕션(트랙 B)은 위 4종에 더해 **langfuse/langfuse:3.130.0**, **langfuse/langfuse-worker:3.130.0**,
> **clickhouse/clickhouse-server:24.8**, **redis:7.4-alpine**,
> **minio/minio:RELEASE.2024-11-07T00-52-20Z** (모두 docker hub 미러)를 추가로 받아야 한다.

### 폐쇄망 pip 미러 (빌드 시 파이썬 패키지)

이미지 빌드 중 `pip install`이 사내 PyPI 미러/프록시를 쓰도록 `.env`에 설정한다(Dockerfile 손대지 않음):

```bash
# .env (사내 pip.conf 값 그대로)
PIP_INDEX_URL=http://repository.samsungds.net/repository/proxy-pypi-files.pythonhosted.org/simple
PIP_TRUSTED_HOST=repository.samsungds.net
BUILD_PROXY=http://202.20.187.241:3128
# 내부 미러는 프록시를 거치지 않고 직접 접근(프록시 경유 시 응답이 깨져 JSONDecodeError가 났던 원인)
NO_PROXY=repository.samsungds.net,localhost,127.0.0.1
```

⚠️ 이전 빌드 실패(`json.decoder.JSONDecodeError: Expecting value: line 1 column 1`)는
**내부 미러 요청이 사내 프록시(BUILD_PROXY)를 타면서 응답이 비거나 깨진 것**이 유력한 원인이다.
→ `NO_PROXY`에 미러 호스트를 넣어 **직접 접근**하게 하면 해결된다. 점검:

```bash
# (프록시 없이) 200 + pip 파일 목록이 나오면 정상
curl -sI http://repository.samsungds.net/repository/proxy-pypi-files.pythonhosted.org/simple/pip/
```

> 위 curl이 프록시 없이 200이면 `NO_PROXY` 방식이 맞다. 반대로 프록시를 통해야만 200이면
> `NO_PROXY`를 비우고 `BUILD_PROXY`만 둔다. 그래도 JSONDecodeError가 계속되면 미러가 pip JSON
> API(PEP 691) 미지원인 경우이니 알려달라(older pip 핀을 build-arg로 추가).

---

## 트랙 A — dev/mock 스모크 테스트 (LLM 불필요, 권장)

mock vLLM(임베딩+LLM 에코)이 자동으로 붙어 `.env`·GPU·키가 필요 없다.

### A-1. 기동
```bash
git clone <repo> AI-Infra-Assistant && cd AI-Infra-Assistant
git checkout main
cp .env.example .env          # 호스트 포트 기본값 포함(agent-server=8500)
docker compose -f docker-compose.dev.yml up -d --build
```

> **호스트 포트**: agent-server는 호스트 **8500**으로 노출된다(8000이 이미 사용 중이라 기본값을 8500으로 둠).
> 다른 포트가 겹치면 `.env`의 `AGENT_PORT`/`ADMIN_PORT`/`OPENWEBUI_PORT`/`PG_PORT`/`*_MCP_PORT`만 빈 포트로 바꾼다.
> 사용 중인 포트 확인:
> ```bash
> ss -tlnp | grep -E ':(3000|5432|8080|8500|8501|8502|8503|8504)\b'
> ```
> mock-vllm은 호스트에 노출하지 않는다(내부망 전용).

### A-2. 기동 확인
```bash
docker compose -f docker-compose.dev.yml ps          # 서비스 Up / db-init·dev-config는 Exit 0
docker compose -f docker-compose.dev.yml logs -f agent-server   # 에러 없나 (Ctrl-C)
curl -s http://localhost:8500/health ; echo
curl -s http://localhost:8500/v1/models ; echo
```

### A-3. memory_db 생성/마이그레이션 확인
```bash
docker compose -f docker-compose.dev.yml exec postgres \
  psql -U agent -d postgres -c "\l" | grep memory_db
docker compose -f docker-compose.dev.yml exec postgres \
  psql -U agent -d memory_db -c "\dt"      # memory_turns / user_memory / conversation_state
```

### A-4. 외부 agent API + 메모리 저장 (`/v1/agent/query`)
```bash
curl -s http://localhost:8500/v1/agent/query -H 'Content-Type: application/json' -d '{
  "user_id":"sg.chon","message":"안녕하세요 테스트입니다","conversation_id":"t1","source":"voc-agent"
}' ; echo
```
→ `{"answer":"…","conversation_id":"t1",...}` (mock은 메시지 에코). 이어서 적재 확인:
```bash
docker compose -f docker-compose.dev.yml exec postgres \
  psql -U agent -d memory_db -c \
  "SELECT id,user_id,conversation_id,source,role,left(content,30) FROM memory_turns ORDER BY id;"
```
→ user/assistant 2행이 `sg.chon` 키로 쌓이면 메모리 파이프라인 정상.

### A-5. VOC 계약 엔드포인트 (`/v1/voc/query`)
```bash
curl -s http://localhost:8500/v1/voc/query -H 'Content-Type: application/json' -d '{
  "voc_info":{
    "voc_id":"2026041616503589073","voc_title":"버튼 동작 오류",
    "system":{"id":"SSTM01420","name":"Service Hub"},
    "requester":{"user_id":"sg.chon","user_name":"전성균"},
    "voc_content":{"text":"등록/답변/평가 버튼이 동작하지 않습니다"}
  },
  "output_option":"html"
}' ; echo
```
→ `{"success":true,"answer":{"content":"…"}}` 이면 계약 정상. (dev에선 `service_hub_mcp_url`이 비어 `similar_voc`는 생략 = 정상)

스트리밍:
```bash
curl -N http://localhost:8500/v1/voc/query -H 'Content-Type: application/json' -d '{
  "voc_info":{"voc_id":"v2","requester":{"user_id":"sg.chon"},"voc_content":{"text":"테스트"}},
  "output_option":"markdown","stream":true}'
```
→ `data: {"delta":…}` 스트림 후 완성 envelope + `data: [DONE]`.

### A-6. 웹 UI (선택)
- 관리자 콘솔: http://localhost:8080 (admin/admin) — 데이터 등록, 설정 확인
- 사용자 웹: http://localhost:3000 — 메시지 전송(mock 에코), 흐름은 `logs`로 확인

### A-7. 정리
```bash
docker compose -f docker-compose.dev.yml down -v    # -v: DB 볼륨까지 삭제
```

**트랙 A로 검증되는 것**: 기동, 마이그레이션(memory_db 포함), 두 API 입출력·스트리밍, 메모리 저장/조회, VOC 계약, MCP 호출 배선.
**dev로 안 되는 것**(→ 트랙 B): 실제 답변 품질/의미검색, 요약 증류 품질, SSH 실행, Service Hub `similar_voc`, Langfuse, Open WebUI 사용자별 헤더.

---

## 트랙 B — 프로덕션 (vLLM 준비 후)

### B-1. `.env`
```bash
cp .env.example .env
# POSTGRES_PASSWORD/ADMIN_*/REDIS_PASSWORD/Langfuse 키, vLLM 주소(VLLM_*), SSH_KEY_PATH 등
```

### B-2. 기동
```bash
docker compose up -d --build
```
추가 이미지: `langfuse/langfuse:3.130.0`, `langfuse/langfuse-worker:3.130.0`, `clickhouse/clickhouse-server:24.8`, `redis:7.4-alpine`, MinIO.

### B-3. 주소 확정
관리자 콘솔 → **설정 탭** → `vllm_*`/`rerank_*`가 실제 서버를 가리키는지 확인(즉시 반영).

### B-4. 기능별 활성화
- **사용자별 메모리(Open WebUI)**: prod compose에 `ENABLE_FORWARD_USER_INFO_HEADERS=true` 존재 → 로그인 이메일→계정 매핑 확인.
- **Langfuse**: 키 넣고 `docker compose up -d agent-server` → Users/Sessions 뷰 확인.
- **SSH 실행 툴(System/Command MCP)**: 기본 비활성.
  1. `SSH_KEY_PATH`(대상 서버 root ssh 키) + 호스트 `/etc/hosts`(예: `202.20.185.100 login05`) 마운트 확인
  2. `scheduler_login_host` 설정(기본 login05)
  3. 관리자 콘솔 **System 탭**에서 툴 토글 ON
  4. 테스트: "hgpu8002 GPU 상태" 질의 → `gpu_status` 실행 확인
- **Service Hub(similar_voc)**: 방화벽 개통 후 설정 탭 `service_hub_mcp_url` 입력 → VOC 요청으로 `similar_voc`/`voc_id` 확인.

---

## 자주 쓰는 명령
```bash
docker compose -f docker-compose.dev.yml logs -f agent-server system-mcp
docker compose -f docker-compose.dev.yml exec postgres psql -U agent -d memory_db
docker compose -f docker-compose.dev.yml restart agent-server
```

## 회귀 테스트(단위)
```bash
pip install pytest
DISABLE_DOCLING=1 CONFIG_DB_DSN=postgresql://x:x@localhost/x \
  PYTHONPATH=shared:admin_console/backend python -m pytest tests/ -v
```
