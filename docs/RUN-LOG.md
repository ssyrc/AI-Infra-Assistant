# 실행 커맨드 로그 (Agent 기동 & 테스트)

> 회사 서버에서 실제로 실행한 커맨드 기준. 새 작업을 할 때마다 이 파일에 이어서 추가한다.

## 1. 이미지 빌드/전송 (오프라인 배포)

인터넷 되는 곳에서 빌드 후 Docker Hub(`ellie0/*`)로 push, 폐쇄망 서버로는 tar로 옮김.

```bash
docker info | grep "Docker Root Dir"
```
도커 저장 위치 확인 (D 드라이브: `/mnt/d/docker-data`).

```bash
RUNTIME_TAG=main-10bc550

docker save \
  ellie0/ai-infra-assistant-db-init:$RUNTIME_TAG \
  ellie0/ai-infra-assistant-mock-vllm:$RUNTIME_TAG \
  ellie0/ai-infra-assistant-mcp:$RUNTIME_TAG \
  ellie0/ai-infra-assistant-agent-server:$RUNTIME_TAG \
  ellie0/ai-infra-assistant-admin-console:$RUNTIME_TAG \
  ellie0/ai-infra-assistant-pgvector:$RUNTIME_TAG \
  ellie0/ai-infra-assistant-postgres:$RUNTIME_TAG \
  ellie0/ai-infra-assistant-open-webui:$RUNTIME_TAG \
  | gzip > /mnt/d/ai-infra-images.tar.gz
```
런타임 이미지 8종을 tar.gz로 저장.

```bash
rsync -avz --progress /mnt/d/ai-infra-images.tar.gz \
  yr9.choi@202.20.185.100:/home/gpu1/yr9.choi/05_halo/
```
폐쇄망 서버로 전송.

## 2. 폐쇄망 서버에서 로드 & 기동

```bash
docker load < ai-infra-images.tar.gz
docker images | grep ai-infra-assistant   # 로드된 태그 확인
```

```bash
docker compose -f docker-compose.dev.yml up -d --no-build
```

**에러**: `No such image: ai-infra-assistant-admin-console:latest`
원인: load된 이미지는 `ellie0/...:main-10bc550` 태그인데, compose는 로컬 태그(`:latest`)를 찾음.

**해결**:
```bash
bash scripts/retag-runtime-images.sh main-10bc550
docker compose -f docker-compose.dev.yml up -d --no-build
```
8개 이미지를 compose용 로컬 태그로 재태깅.

## 3. 기동 확인

```bash
docker compose -f docker-compose.dev.yml ps
curl http://localhost:8500/health
```
→ 전체 서비스 Up, `{"status":"ok",...}` 확인됨.

## 4. 코드만 바뀔 때 (재빌드 없이 재기동)

```bash
git pull origin main
bash scripts/restart-mounted.sh
```

## 5. 코드 반영 (WSL → rsync → 폐쇄망 서버)

로컬 수정 없이 원격 main을 그대로 받아서 서버로 올리는 방식.

```bash
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
```

```bash
rsync -avz --delete --progress /home/yrc/AI-Infra-Assistant/ \
  yr9.choi@202.20.185.100:/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/
```

## 6. 호스트 포트 8500번대로 재배치 (확인됨)

8080/8100/8200/3000/3001/5432 대신 8500부터 순서대로 배정
(`docker-compose.dev.yml`/`docker-compose.yml`/`.env.example`).

```bash
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml ps
```
→ 관리자 콘솔 `:8501`, 사용자 웹 `:8502` 접속 확인됨.

## 7. admin_console vendor 파일 받기 (WSL, 인터넷 환경)

```bash
unset CURL_CA_BUNDLE SSL_CERT_FILE SSL_CERT_DIR   # 1차: 죽은 인증서 경로 에러 해결
cd admin_console/frontend/vendor
curl -L -o react.production.min.js https://unpkg.com/react@18/umd/react.production.min.js
curl -L -o react-dom.production.min.js https://unpkg.com/react-dom@18/umd/react-dom.production.min.js
curl -L -o babel.min.js https://unpkg.com/@babel/standalone@7/babel.min.js
```
2차 시도에서 `-L` 없이 받아서 `Uncaught SyntaxError: Unexpected identifier 'to'` 발생 —
unpkg.com이 버전 없는 URL을 302로 리다이렉트하는데 `-L` 없이 받으면 "Redirecting to ..." 안내문
자체가 파일로 저장됨(프록시 문제 아니었음). `-L`로 해결.

3차 시도(babel만 무버전 `@babel/standalone`): 파일은 제대로 받아졌는데 브라우저에서
`Cannot use import statement outside a module`. 원인: 버전 미고정이라 최신 메이저(8.x)가
잡혔고, 8.x의 `transformScriptTags()`가 이 프로젝트의 UMD 전역 + non-module
`<script type="text/babel">` 구성과 안 맞음. `@babel/standalone@7`로 고정해서 해결
(`admin_console/frontend/vendor/README.md`에도 반영).

## 8. MCP 세션 연결 문제 (원인 찾고 코드로 고침)

챗 요청 시 재발:
```
ConnectionError: Failed to create MCP session: Connection closed
```
MCP 컨테이너 로그에서 진짜 원인 확인:
```
manual-mcp-1  | INFO: 172.21.0.4:41786 - "POST /mcp HTTP/1.1" 421 Misdirected Request
manual-mcp-1  | Invalid Host header: manual-mcp:8001
```
`mcp_servers/*/server.py`의 `FastMCP("manual-mcp", stateless_http=True)`가 `host`를 안 넘겨서
mcp SDK 기본값 `127.0.0.1`로 잡히고, 그러면 SDK가 DNS-rebinding 보호를 자동으로 켜서
Host 헤더를 `127.0.0.1`/`localhost`만 허용한다. 실제로는 `uvicorn.run(host="0.0.0.0")`로 띄우고
도커 네트워크 이름(`manual-mcp:8001` 등)으로 붙기 때문에 전부 421로 막힌 것.
4개 서버 전부 `FastMCP(..., host="0.0.0.0")`로 고쳐서 push함.

**확인됨**: 코드 반영 후 open-webui 챗이 에러 없이 정상 응답(`[mock-llm] 다음 요청을 받았습니다: ...`).
MCP 로그에 남아있는 421은 컨테이너를 재생성이 아니라 재시작만 해서 예전 로그 버퍼가 안 지워진 것뿐,
실제로는 더 이상 안 남.

## 9. 관리자 콘솔 설정 탭에서 vLLM 주소 입력 (완료, 실제 vLLM은 아직 미기동)

`http://202.20.183.30:8501` 설정 탭에서 agent 서버(202.20.183.30), LLM 서버(75.23.32.41) 주소 입력.
챗 응답이 아직 `[mock-llm] ...` 포맷인 걸 보면 hgpu4041에 실제 vLLM은 아직 안 띄운 상태.

## 10. hgpu4041 — 모델 다운로드/전송/이미지 pull (완료)

```bash
pip install -U huggingface_hub
huggingface-cli download Qwen/Qwen3-235B-A22B-Instruct-2507-FP8 --local-dir ./models/Qwen3-235B-A22B-Instruct-2507-FP8
huggingface-cli download Qwen/Qwen3-Embedding-8B --local-dir ./models/Qwen3-Embedding-8B
rsync -avz --progress ./models/Qwen3-235B-A22B-Instruct-2507-FP8 yr9.choi@75.23.32.41:/home/gpu1/yr9.choi/halo_workspace/models/
rsync -avz --progress ./models/Qwen3-Embedding-8B yr9.choi@75.23.32.41:/home/gpu1/yr9.choi/halo_workspace/models/
docker pull repo.samsungds.net/docker.io/vllm/vllm-openai:latest
```
hgpu4041에서 `ls`로 두 모델 디렉토리 안 파일(config.json, safetensors 등) 확인됨.

## 11. vLLM 기동 에러 — 진짜 원인은 경로 오타 (해결)

```
huggingface_hub.errors.HFValidationError: Repo id must be in the form 'repo_name' or 'namespace/repo_name'
OSError: Can't load the configuration of '/workspace/models/Qwen3-235B-A22B-Instruct-2507-FP8'
```
SELinux일 거라 추정했었는데, 실제 원인은 모델을 옮긴 실제 경로가
`/home/gpu1/yr9.choi/halo_workspace/models`가 아니라 `/home/gpu1/yr9.choi/05_halo/models`였던 것
(rsync 타깃 경로가 실제 위치와 달랐음). `-v` 경로를 고치니 해결.

## 12. vLLM LLM/임베딩 기동 (완료)

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

---

## 다음 항목은 이어서 여기 아래에 추가
