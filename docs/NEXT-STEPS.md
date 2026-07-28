# 지금 할 일

## 지금 당장

### 1) 임베딩 모델을 Qwen3-Embedding-8B → BAAI/bge-m3로 변경 (결정됨)

8B는 GPU에 자리가 없어서(가중치만 GPU의 75% 차지) 포기. `bge-m3`(5.68억 파라미터, 출력 차원
1024)로 변경 — 이 프로젝트의 DB 스키마(`vector(1024)`)와 기본 설정값이 원래 `bge-m3` 기준으로
설계돼 있어서 딱 맞다. 모델 준비 + 기동:
```bash
huggingface-cli download BAAI/bge-m3 --local-dir ./models/bge-m3
rsync -avz --progress ./models/bge-m3 yr9.choi@75.23.32.41:/home/gpu1/yr9.choi/05_halo/models/
```
hgpu4041에서:
```bash
docker rm -f serve-vllm-embed 2>/dev/null
docker run -dit --rm --gpus '"device=0"' --network host --ipc host \
    -v /home/gpu1/yr9.choi/05_halo/models:/workspace/models \
    --name serve-vllm-embed repo.samsungds.net/docker.io/vllm/vllm-openai:latest \
    --model /workspace/models/bge-m3 \
    --task embed --gpu-memory-utilization 0.08 \
    --port 8010 --served-model-name bge-m3
```
```bash
docker logs serve-vllm-embed --tail 50
```
안 뜨면(메모리 부족 등) `nvidia-smi`와 로그를 그대로 Errors에 붙여줘.

### 2) 리랭커 모델 준비 (아직 안 했으면, 기존 계획 그대로 — 변경 없음)
```bash
huggingface-cli download BAAI/bge-reranker-v2-m3 --local-dir ./models/bge-reranker-v2-m3
rsync -avz --progress ./models/bge-reranker-v2-m3 yr9.choi@75.23.32.41:/home/gpu1/yr9.choi/05_halo/models/
```
```bash
docker run -dit --rm --gpus '"device=1"' --network host --ipc host \
    -v /home/gpu1/yr9.choi/05_halo/models:/workspace/models \
    --name serve-vllm-rerank repo.samsungds.net/docker.io/vllm/vllm-openai:latest \
    --model /workspace/models/bge-reranker-v2-m3 \
    --task score --gpu-memory-utilization 0.08 \
    --port 8020 --served-model-name bge-reranker-v2-m3
```

### 3) 도달 확인
```bash
curl http://75.23.32.41:8000/v1/models
curl http://75.23.32.41:8010/v1/models
curl http://75.23.32.41:8020/v1/models
```

### 4) admin_console 설정 탭 (임베딩/리랭커 뜨면 저장)

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

### 5) 코드 최신화 (코드 바뀔 때마다)
```bash
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
rsync -avz --delete --exclude '.env' --progress /home/yrc/AI-Infra-Assistant/ \
  yr9.choi@202.20.185.100:/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/
```
백엔드/프론트만 바뀌었으면(신규 pip 의존성 없으면) 재시작만:
```bash
docker compose -f docker-compose.dev.yml restart admin-console
```
`requirements.txt`가 바뀐 경우만 재빌드:
```bash
docker compose -f docker-compose.dev.yml build admin-console
docker compose -f docker-compose.dev.yml up -d
```

### 6) 확인 & 기능 테스트
```bash
curl http://202.20.183.30:8500/v1/models   # qwen3-235b-a22b 나오는지
```
- open-webui (`:8502`) 채팅 → 실제 응답 확인
- admin_console 메모리/RAG 기능 → 임베딩/리랭커 연결 확인

완료된 내역/원인 분석은 `docs/RUN-LOG.md` 참고.
