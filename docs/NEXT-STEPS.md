# 지금 할 일

## ⚡ 지금 당장 (순서대로)

### 0) hgpu4041 — CUDA busy는 지금 GPU 다 비어있음, 그냥 재시도
`nvidia-smi` 확인 결과 GPU 0/2/3는 프로세스 없이 완전히 비어있고(1MiB만 사용, 드라이버 상주분),
GPU 1은 리랭커(`VLLM::EngineCore`)만 정상적으로 붙어있다. 좀비 프로세스가 GPU를 붙들고 있는
상태가 아니므로, 아까 에러는 일시적이었을 가능성이 높다. `docker ps -a`에도 llm/embed 컨테이너가
없으니(생성 자체가 안 됐거나 이미 지움) 이름 충돌 걱정 없이 그냥 다시 실행하면 된다:
```bash
docker run -dit --rm --gpus all --network host --ipc host \
    -v /home/gpu1/yr9.choi/05_halo/models:/workspace/models \
    --name serve-vllm-llm repo.samsungds.net/docker.io/vllm/vllm-openai:latest \
    --model /workspace/models/Qwen3-235B-A22B-Instruct-2507-FP8 \
    --tensor-parallel-size 4 --gpu-memory-utilization 0.85 \
    --port 8000 --served-model-name qwen3-235b-a22b

docker run -dit --rm --gpus '"device=0"' --network host --ipc host \
    -v /home/gpu1/yr9.choi/05_halo/models:/workspace/models \
    --name serve-vllm-embed repo.samsungds.net/docker.io/vllm/vllm-openai:latest \
    --model /workspace/models/Qwen3-Embedding-8B \
    --task embed --gpu-memory-utilization 0.15 \
    --port 8010 --served-model-name qwen3-embedding-8b
```
(`--rm`을 다시 붙였다 — 실패해도 컨테이너가 안 남아서 다음 재시도가 깔끔함.) 또 busy 뜨면
`docker logs serve-vllm-llm`으로 어느 GPU에서 나는 에러인지 확인해서 알려줘.

### 1) 에이전트 서버(202.20.183.30) — `.env` 확인됨: 삭제됨 → 복구
`cat .env` 결과 `No such file or directory` — 예상대로 rsync `--delete`가 지운 것. 다행히
`.env.example`에 이미 사내 미러 실제 값이 CHANGE_ME 없이 채워져 있고(`REGISTRY_DOCKERHUB`,
`REGISTRY_GHCR`, `PIP_INDEX_URL`, `BUILD_PROXY`, `APT_MIRROR` 등), dev 트랙은 비밀번호도
`docker-compose.dev.yml`에 `devpass`/`admin`/`admin`으로 고정돼 있어서 `.env`의 CHANGE_ME
값들과 무관하다. 그냥 복사만 하면 됨:
```bash
cp /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/.env.example \
   /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/.env
```

**앞으로는 반드시 `--exclude '.env'`를 붙인다** (아래 3번부터 반영됨).

### 2) hgpu4041 — 리랭커 (완료, GPU 1에 이미 떠 있음)
`BAAI/bge-reranker-v2-m3`를 vLLM `--task score`로 GPU 1에 띄운 게 이미 정상 동작 중(vLLM의
Cohere 호환 `/rerank` 라우트 노출). LLM/임베딩만 0번 재시도하면 됨.

### 3) 도달 확인
```bash
curl http://75.23.32.41:8000/v1/models
curl http://75.23.32.41:8010/v1/models
curl http://75.23.32.41:8020/v1/models
```

### 4) 에이전트 서버(202.20.183.30) — 최신 코드 반영 (⚠️ 이번엔 재빌드 필요, `.env` 보존)
WSL에서:
```bash
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
```
서버로 반영 (**`.env` 지우지 않게 `--exclude` 필수**):
```bash
rsync -avz --delete --exclude '.env' --progress /home/yrc/AI-Infra-Assistant/ \
  yr9.choi@202.20.185.100:/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/
```
계정 관리 기능에 `bcrypt`가 새 의존성으로 추가돼서 admin-console은 **재시작만으론 안 되고
이미지를 다시 빌드**해야 함(mounted 코드 재시작으론 pip 패키지가 안 깔림). 0번에서 `.env`를
복구했는지 먼저 확인한 다음:
```bash
docker compose -f docker-compose.dev.yml build admin-console
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml ps
curl http://localhost:8500/health
```

### 5) admin_console 설정 탭

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

### 6) 확인 & 기능 테스트
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
- 설정 탭 "MCP 엔드포인트" 그룹에 왜 내부망 이름을 그대로 둬야 하는지 경고 카드 추가.
- VOC 이력 엑셀 업로드 재작성: 4행부터 헤더, 의뢰내용/조치일/처리내용/만족도만 사용, 조치일·
  처리내용 있는 행만, 만족도 "불만족"·"매우불만족" 제외, 본문 HTML은 태그만 제거하고 내용
  (명령어/코드 포함)은 그대로 보존.

완료/과거 내역은 `docs/RUN-LOG.md` 참고.
