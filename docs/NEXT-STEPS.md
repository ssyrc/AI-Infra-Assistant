# 지금 할 일

## ⚡ 지금 당장 (순서대로)

### 1) hgpu4041 — vLLM 마운트 확인 (현재 에러 원인)
`--model /workspace/models/...`을 huggingface_hub가 로컬 경로가 아니라 Hub repo id로 오해해서
`HFValidationError`/`Can't load the configuration`가 났다. transformers는 그 경로에
`os.path.isdir()`가 **False**일 때만 이렇게 된다 — 즉 컨테이너 안에서 마운트가 안 보이는 것이다.
호스트에서는 `ls`로 파일이 다 보였으니(→ 호스트 경로는 맞음), 컨테이너 쪽 마운트 문제다.

먼저 컨테이너 안에서 실제로 보이는지 확인:
```bash
docker run --rm --gpus all --network host --ipc host \
  -v /home/gpu1/yr9.choi/halo_workspace/models:/workspace/models \
  --entrypoint ls repo.samsungds.net/docker.io/vllm/vllm-openai:latest -la /workspace/models
```
비어 있거나 에러면 SELinux가 바인드 마운트를 막고 있을 가능성이 높다(RHEL/CentOS 계열에서 아주
흔함). `-v` 뒤에 `:Z`를 붙여서 재시도(아래 LLM/임베딩 기동 커맨드에 이미 반영):
```bash
docker run --rm --gpus all --network host --ipc host \
  -v /home/gpu1/yr9.choi/halo_workspace/models:/workspace/models:Z \
  --entrypoint ls repo.samsungds.net/docker.io/vllm/vllm-openai:latest -la /workspace/models
```
이래도 안 보이면 `getenforce`로 SELinux 상태 확인하고, dockerd가 그 경로(NFS 홈일 수도 있음)에
실제로 접근 가능한지 확인 필요 — 결과 알려주면 다음 단계 짚어줄게.

### 2) hgpu4041 — LLM 기동 (`:Z` 추가됨)
```bash
docker run -dit --rm --gpus all --network host --ipc host \
    -v /home/gpu1/yr9.choi/halo_workspace/models:/workspace/models:Z \
    --name serve-vllm-llm repo.samsungds.net/docker.io/vllm/vllm-openai:latest \
    --model /workspace/models/Qwen3-235B-A22B-Instruct-2507-FP8 \
    --tensor-parallel-size 4 --gpu-memory-utilization 0.85 \
    --port 8000 --served-model-name qwen3-235b-a22b
```

### 3) hgpu4041 — 임베딩 기동 (`:Z` 추가됨)
```bash
docker run -dit --rm --gpus '"device=0"' --network host --ipc host \
    -v /home/gpu1/yr9.choi/halo_workspace/models:/workspace/models:Z \
    --name serve-vllm-embed repo.samsungds.net/docker.io/vllm/vllm-openai:latest \
    --model /workspace/models/Qwen3-Embedding-8B \
    --task embed --gpu-memory-utilization 0.15 \
    --port 8010 --served-model-name qwen3-embedding-8b
```
OOM 나면 `--gpu-memory-utilization` 값을 LLM/임베딩 양쪽에서 조절.

### 4) 도달 확인
```bash
curl http://75.23.32.41:8000/v1/models
curl http://75.23.32.41:8010/v1/models
```

### 5) 에이전트 서버(202.20.183.30) — 최신 코드 반영 (System MCP 커맨드 추가 기능 포함)
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
반영 후 재기동:
```bash
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml ps
curl http://localhost:8500/health
```

### 6) admin_console 설정 탭 — vLLM 주소만 실제 GPU 서버로

> **설정 탭 값 정리**: `vllm_llm_base_url`/`vllm_embed_base_url`만 `75.23.32.41`(GPU 서버)로
> 바꾸면 된다. `manual_mcp_url`/`command_mcp_url`/`voc_mcp_url`/`system_mcp_url`은 **바꾸지 마세요**
> — 이건 agent-server가 도커 내부망으로 MCP 컨테이너에 붙는 주소(`http://command-mcp:8002/mcp`
> 같은 형태)라서 외부 IP(202.20.183.30/75.23.32.41)로 바꾸면 오히려 연결이 끊깁니다. 전부 같은
> `docker-compose.dev.yml` 스택 안에 있어서 내부 이름으로만 통신합니다.

`http://202.20.183.30:8501` → 설정 탭 (저장 즉시 반영, 재시작 불필요):

| 키 | 값 |
|---|---|
| `vllm_llm_base_url` | `http://75.23.32.41:8000/v1` |
| `vllm_llm_model` | `qwen3-235b-a22b` |
| `vllm_embed_base_url` | `http://75.23.32.41:8010/v1` |
| `vllm_embed_model` | `qwen3-embedding-8b` |

⚠️ `docker compose -f docker-compose.dev.yml down` 후 다시 `up` 하면 `dev-config`가 이 값을
mock으로 재덮어씀. 컨테이너를 살려둔 채로만 설정 변경.

### 7) 확인 & 기능 테스트
```bash
curl http://202.20.183.30:8500/v1/models   # qwen3-235b-a22b 나오는지
```
- open-webui (`:8502`) 채팅 → 실제 응답 확인 (mock 에코가 아닌지)
- admin_console 메모리/RAG 기능 → 임베딩 연결 확인

---

## 다음에 손볼 것 (System MCP 콘솔 UI, 아직 미착수)

- 화이트리스트 탭에서 `gpu_status`처럼 이름만 보이는데 **실제 실행되는 커맨드도 같이 보이게**.
- "커맨드 추가"를 별도 탭으로 두지 말고, 화이트리스트 탭의 "추가" 버튼 → 모달(팝업)에서 등록하도록 재구성.

완료/과거 내역은 `docs/RUN-LOG.md` 참고.
