# 지금 할 일

## ⚡ 지금 당장 (순서대로)

### 1) hgpu4041 — 리랭커도 기동 (모델 다운로드 중이라고 하셨음)
LLM/임베딩은 이미 떴다(경로가 `halo_workspace/models`가 아니라 `05_halo/models`였던 게 원인).
리랭커는 `BAAI/bge-reranker-v2-m3`를 vLLM `--task score`로 띄우면 된다(vLLM의 Cohere 호환
`/rerank` 라우트 노출). GPU 1장에 여유 있는 만큼만 필요:
```bash
docker run -dit --rm --gpus '"device=0"' --network host --ipc host \
    -v /home/gpu1/yr9.choi/05_halo/models:/workspace/models \
    --name serve-vllm-rerank repo.samsungds.net/docker.io/vllm/vllm-openai:latest \
    --model /workspace/models/bge-reranker-v2-m3 \
    --task score --gpu-memory-utilization 0.1 \
    --port 8020 --served-model-name bge-reranker-v2-m3
```
> vLLM 버전(0.10.1.1)에 따라 `--task score` 플래그명이 다를 수 있다. 기동 로그에
> `/rerank` 또는 `/v1/score` 라우트가 뜨는지로 확인.

### 2) 도달 확인
```bash
curl http://75.23.32.41:8000/v1/models
curl http://75.23.32.41:8010/v1/models
curl http://75.23.32.41:8020/v1/models
```

### 3) 에이전트 서버(202.20.183.30) — 최신 코드 반영 (⚠️ 이번엔 재빌드 필요)
WSL에서:
```bash
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
```
서버로 반영:
```bash
rsync -avz --delete --progress /home/yrc/AI-Infra-Assistant/ \
  yr9.choi@202.20.185.100:/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/
```
계정 관리 기능에 `bcrypt`가 새 의존성으로 추가돼서 admin-console은 **재시작만으론 안 되고
이미지를 다시 빌드**해야 함(mounted 코드 재시작으론 pip 패키지가 안 깔림):
```bash
docker compose -f docker-compose.dev.yml build admin-console
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml ps
curl http://localhost:8500/health
```

### 4) admin_console 설정 탭

`http://202.20.183.30:8501` → 설정 탭 (저장 즉시 반영, 재시작 불필요):

| 키 | 값 |
|---|---|
| `vllm_llm_base_url` | `http://75.23.32.41:8000/v1` |
| `vllm_llm_model` | `qwen3-235b-a22b` |
| `vllm_embed_base_url` | `http://75.23.32.41:8010/v1` |
| `vllm_embed_model` | `qwen3-embedding-8b` |
| `rerank_provider` | `vllm` |
| `rerank_base_url` | `http://75.23.32.41:8020` |
| `rerank_model` | `bge-reranker-v2-m3` |

`manual_mcp_url`/`command_mcp_url`/`voc_mcp_url`/`system_mcp_url`은 **바꾸지 않습니다** — 이미
`docker-compose.dev.yml`에서 agent-server와 같은 컨테이너 네트워크에 있는 MCP들 이름
(`http://command-mcp:8002/mcp`)이고, 실제로 챗이 정상 응답한 것 자체가 이 연결이 이미 되고 있다는
증거다. 콘솔 설정 탭에 왜 이게 맞는 값인지 설명을 추가해뒀다(재확인 원하면 아래 검증 커맨드).

검증(agent-server 컨테이너 안에서 MCP가 실제로 붙는지 직접 확인):
```bash
docker compose -f docker-compose.dev.yml exec agent-server \
  curl -s -o /dev/null -w '%{http_code}\n' http://command-mcp:8002/mcp
```
(MCP 프로토콜상 GET에는 4xx가 정상 — 여기서 보고 싶은 건 "연결 자체가 되는지"이지 200 여부가 아님)

⚠️ `docker compose -f docker-compose.dev.yml down` 후 다시 `up` 하면 `dev-config`가 vLLM 값을
mock으로 재덮어씀. 컨테이너를 살려둔 채로만 설정 변경.

### 5) 확인 & 기능 테스트
```bash
curl http://202.20.183.30:8500/v1/models   # qwen3-235b-a22b 나오는지
```
- open-webui (`:8502`) 채팅 → 실제 응답 확인 (mock 에코가 아닌지)
- admin_console 메모리/RAG 기능 → 임베딩/리랭커 연결 확인

---

## 완료 (이번 배포에 포함됨, 반영만 하면 됨)

- System MCP 탭: "커맨드 추가" 서브탭 없애고 화이트리스트 탭 "추가" 버튼 → 모달로 통합,
  실제 커맨드 노출, 필요 역할 "전체 허용/admin 전용" 선택으로 변경.
- 계정 관리 탭 신설(admin 계정 여러 개 관리, `.env` 기본 계정은 잠금 방지용으로 항상 유효).

완료/과거 내역은 `docs/RUN-LOG.md` 참고.
