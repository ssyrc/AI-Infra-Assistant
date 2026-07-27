# 지금 할 일 — vLLM 연결 (hgpu4041 / 75.23.32.41, 4x H100 80GB)

LLM: `Qwen3-235B-A22B-Instruct-2507` (FP8) · 임베딩: `Qwen3-Embedding-8B`

## 1. 인터넷 되는 곳에서 모델 다운로드

```bash
pip install -U huggingface_hub
huggingface-cli download Qwen/Qwen3-235B-A22B-Instruct-2507-FP8 \
  --local-dir ./models/Qwen3-235B-A22B-Instruct-2507-FP8
huggingface-cli download Qwen/Qwen3-Embedding-8B \
  --local-dir ./models/Qwen3-Embedding-8B
```

## 2. hgpu4041로 전송

```bash
rsync -avz --progress ./models/Qwen3-235B-A22B-Instruct-2507-FP8 \
  yr9.choi@75.23.32.41:/home/gpu1/yr9.choi/halo_workspace/models/
rsync -avz --progress ./models/Qwen3-Embedding-8B \
  yr9.choi@75.23.32.41:/home/gpu1/yr9.choi/halo_workspace/models/
```

## 3. hgpu4041에서 vLLM 이미지 pull (사내 미러)

```bash
docker pull repo.samsungds.net/docker.io/vllm/vllm-openai:latest
```

## 4. LLM 기동 (4장 전체, TP=4)

```bash
docker run -dit --rm --gpus all \
    --network host \
    --ipc host \
    -v /home/gpu1/yr9.choi/halo_workspace/models:/workspace/models \
    --name serve-vllm-llm repo.samsungds.net/docker.io/vllm/vllm-openai:latest \
    --model /workspace/models/Qwen3-235B-A22B-Instruct-2507-FP8 \
    --tensor-parallel-size 4 \
    --gpu-memory-utilization 0.85 \
    --port 8000 \
    --served-model-name qwen3-235b-a22b
```
`--gpu-memory-utilization 0.85`로 GPU당 여유를 남겨서 임베딩 프로세스가 같은 GPU에 곁들여 돌 수 있게 함.

## 5. 임베딩 기동 (여유 VRAM에 GPU 1장만 지정)

```bash
docker run -dit --rm --gpus '"device=0"' \
    --network host \
    --ipc host \
    -v /home/gpu1/yr9.choi/halo_workspace/models:/workspace/models \
    --name serve-vllm-embed repo.samsungds.net/docker.io/vllm/vllm-openai:latest \
    --model /workspace/models/Qwen3-Embedding-8B \
    --task embed \
    --gpu-memory-utilization 0.15 \
    --port 8010 \
    --served-model-name qwen3-embedding-8b
```
OOM 나면 `--gpu-memory-utilization` 값을 LLM/임베딩 양쪽에서 조절.

## 6. 도달 확인

```bash
curl http://75.23.32.41:8000/v1/models
curl http://75.23.32.41:8010/v1/models
```

## 7. admin_console 설정 탭에서 연결

`http://<agent서버>:8501` → 설정 탭 (저장 즉시 반영, 재시작 불필요):

| 키 | 값 |
|---|---|
| `vllm_llm_base_url` | `http://75.23.32.41:8000/v1` |
| `vllm_llm_model` | `qwen3-235b-a22b` |
| `vllm_embed_base_url` | `http://75.23.32.41:8010/v1` |
| `vllm_embed_model` | `qwen3-embedding-8b` |

⚠️ `docker compose -f docker-compose.dev.yml down` 후 다시 `up` 하면 `dev-config`가 이 값을 mock으로 재덮어씀. 컨테이너를 살려둔 채로만 설정 변경.

## 8. 확인 & 기능 테스트

```bash
curl http://<agent서버>:8500/v1/models   # qwen3-235b-a22b 나오는지
```
- open-webui (`:8502`) 채팅 → 실제 응답 확인
- admin_console 메모리/RAG 기능 → 임베딩 연결 확인
