# 지금 할 일

## 지금 당장

### 1) hgpu4041 — LLM에 tool-calling 옵션 켜서 재기동 (필수)

```
litellm.BadRequestError: ... '"auto" tool choice requires --enable-auto-tool-choice and
--tool-call-parser to be set'
```
이 에이전트는 MCP 툴콜 기반이라 매 요청 `tool_choice: "auto"`를 보내는데, vLLM은 이 옵션을 켜야
지원한다. Qwen3 계열은 `hermes` 파서를 쓴다:
```bash
docker rm -f serve-vllm-llm
docker run -dit --rm --gpus all --network host --ipc host \
    -v /home/gpu1/yr9.choi/05_halo/models:/workspace/models \
    --name serve-vllm-llm repo.samsungds.net/docker.io/vllm/vllm-openai:latest \
    --model /workspace/models/Qwen3-235B-A22B-Instruct-2507-FP8 \
    --tensor-parallel-size 4 --gpu-memory-utilization 0.85 \
    --max-model-len 32768 \
    --enable-auto-tool-choice --tool-call-parser hermes \
    --port 8000 --served-model-name qwen3-235b-a22b
```
```bash
docker logs serve-vllm-llm --tail 50
```
안 뜨거나 툴콜이 여전히 깨지면(예: 파서가 모델 출력 포맷과 안 맞는 에러) 로그 그대로 보내줘 —
`--chat-template` 지정이 추가로 필요할 수 있음.

### 2) Open WebUI 기본 모델 고정

Open WebUI는 우리 백엔드가 노출하는 모델(현재 하나뿐)을 "기본 모델"로 자동 고정하지 않고,
브라우저별 마지막 선택을 기억하거나 admin 설정을 따른다 — 코드로 강제할 수 있는 부분이 아니라
Open WebUI 쪽 설정임:

`:8502` 접속 → 우측 상단 프로필 → **관리자 패널 → 설정 → 모델** (또는 일반 설정) → **기본 모델**을
지금 뜨는 모델(mock이면 `mock-llm`, 실제 백엔드면 `AI Infra Assistant`)로 선택.
mock ↔ 실제 백엔드를 바꿀 때마다 노출되는 모델 id 자체가 바뀌므로, 바꿀 때마다 여기서 한 번씩
다시 선택해줘야 함(자동 동기화는 Open WebUI admin API 연동이 필요한 별도 작업이라 필요하면
말해줘).

### 3) 코드 최신화 (아직 안 했으면 — 모델명 표시 버그 수정 + Arena 모델 제거)
```bash
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
rsync -avz --delete --exclude '.env' --progress /home/yrc/AI-Infra-Assistant/ \
  yr9.choi@202.20.185.100:/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/
```
```bash
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml restart agent-server open-webui
```

### 4) admin_console 설정 탭 — LLM/임베딩/리랭커 값 (아직 저장 안 했으면)

`http://202.20.183.30:8501` → 설정 탭:

| 키 | 값 |
|---|---|
| `vllm_llm_base_url` | `http://75.23.32.41:8000/v1` |
| `vllm_llm_model` | `qwen3-235b-a22b` |
| `vllm_embed_base_url` | `http://75.23.32.41:8010/v1` |
| `vllm_embed_model` | `bge-m3` |
| `rerank_provider` | `vllm` |
| `rerank_base_url` | `http://75.23.32.41:8020` |
| `rerank_model` | `bge-reranker-v2-m3` |

⚠️ `docker compose -f docker-compose.dev.yml down` 후 `up`하면 `dev-config`가 이 값들을 mock으로
재덮어씀. 컨테이너를 살려둔 채로만 설정 변경.

### 5) 확인
```bash
curl http://202.20.183.30:8500/v1/models
```
open-webui(`:8502`)에서 실제 채팅 + 매뉴얼/시스템 조회 등 툴콜 필요한 질문으로 테스트.

완료된 내역/원인 분석은 `docs/RUN-LOG.md` 참고.
