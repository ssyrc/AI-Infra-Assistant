# 지금 할 일 — vLLM 연결 (hgpu4041 / 75.23.32.41, 4x H100 80GB)

LLM: `Qwen3-235B-A22B-Instruct-2507` (FP8) · 임베딩: `Qwen3-Embedding-8B`

## 0. 코드 반영 + 포트 재기동 + admin_console 화면 깨짐 고치기 (먼저)

WSL에서:
```bash
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
```
`.env`에 옛 포트 값(`ADMIN_PORT=8080`, `OPENWEBUI_PORT=3000`, `PG_PORT=5432`, `MANUAL_MCP_PORT=8501` 등)이
박혀 있으면 지우거나 새 기본값(8501~8507)으로 바꾈 것. 없으면 그대로 둬도 새 기본값 적용됨.

admin_console 화면 검은화면(`vendor/react.production.min.js` 로드 실패) 원인은 vendor 파일 누락.
인터넷 되는 곳에서 받아서 서버로 옮기기:
```bash
# curl이 "error setting certificate file: ..." 로 죽으면 죽은 인증서 경로가 env에 남아있는 것
unset CURL_CA_BUNDLE SSL_CERT_FILE SSL_CERT_DIR
# 사내 프록시를 안 거치면 unpkg.com이 차단 페이지를 돌려줄 수 있다(빌드에 쓰는 프록시 재사용)
export https_proxy=http://202.20.187.241:3128 http_proxy=http://202.20.187.241:3128

cd admin_console/frontend/vendor
curl -o react.production.min.js https://unpkg.com/react@18/umd/react.production.min.js
curl -o react-dom.production.min.js https://unpkg.com/react-dom@18/umd/react-dom.production.min.js
curl -o babel.min.js https://unpkg.com/@babel/standalone/babel.min.js

# 받은 파일이 진짜 JS인지 확인 (사람이 읽을 수 있는 문장이 보이면 차단 페이지가 저장된 것)
head -c 300 react.production.min.js react-dom.production.min.js babel.min.js
```
받은 3개 파일 + 최신 코드를 서버로 반영:
```bash
rsync -avz --delete --progress /home/yrc/AI-Infra-Assistant/ \
  yr9.choi@202.20.185.100:/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/
```

컨테이너 재기동 (포트 매핑 반영):
```bash
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml ps
curl http://localhost:8500/health
```
- 관리자 콘솔: http://<서버IP>:8501 (화면 정상 뜨는지, admin/admin)
- 사용자 웹: http://<서버IP>:8502

open-webui 채팅에서 `Failed to create MCP session: Connection closed`가 뜨면 — `up -d`로 MCP
컨테이너만 재생성되고 agent-server는 옛 연결을 붙들고 있어서 그렇다. 전부 같이 재시작:
```bash
bash scripts/restart-mounted.sh
```
(Open WebUI가 띄우는 `WebUI could not connect to Ollama` 500 에러는 무시해도 됨 — 이 프로젝트는
Ollama를 안 쓰고 agent-server의 OpenAI 호환 API만 쓴다.)

open-webui 채팅에서 `/api/chat/completions`가 `400 Bad Request`면 — 브라우저 콘솔엔 에러 본문이
안 보이니 agent-server를 직접 찔러서 실제 원인을 확인:
```bash
curl -s http://localhost:8500/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"mock-llm","messages":[{"role":"user","content":"안녕"}]}'
docker compose -f docker-compose.dev.yml logs --tail=50 agent-server
```

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
