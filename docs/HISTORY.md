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

## 13. vLLM LLM KV 캐시 부족 원인 확정 및 수정 (완료, 문서화)

```
ValueError: To serve at least one request with the models's max seq len (262144),
(11.75 GiB KV cache is needed, which is larger than the available KV cache memory (8.03 GiB).
```
Qwen3-235B-A22B의 기본 최대 컨텍스트(262144 토큰) 때문에 `--gpu-memory-utilization 0.85`로
남는 KV 캐시 용량이 부족해서 실패. 이 에이전트는 256K 컨텍스트가 필요 없으므로
`--max-model-len 32768`을 LLM `docker run`에 추가하는 것으로 해결(`docs/NEXT-STEPS.md` 0번).

## 14. 설정 탭 / System MCP 탭에 "지금 재시작" 버튼 추가 (완료)

`platform_settings`에서 `hot_reload=false`인 값(MCP URL, 시스템 프롬프트 등)을 저장하거나
System MCP 화이트리스트/커스텀 커맨드를 바꾸면, 해당 서비스를 바로 재시작할 수 있는 버튼이
나타나도록 구현:
- 백엔드: `admin_console/backend/routers/ops.py` 신설 —
  `POST /api/ops/restart/{service}`가 `docker` Python SDK(docker-py)로
  허용 목록(`agent-server`/`manual-mcp`/`command-mcp`/`voc-mcp`/`system-mcp`)의 컨테이너만
  `restart()` 호출. 호스트 `/var/run/docker.sock`을 admin-console 컨테이너에 바인드 마운트해야
  동작(`docker-compose.dev.yml`/`docker-compose.yml` 양쪽에 추가, 코멘트로 보안 트레이드오프
  명시).
- 프론트: `RESTART_SERVICE_FOR_KEY` 매핑 + `restartService()` 헬퍼(확인창 → API 호출 → 결과
  알림)를 설정 탭과 System MCP 탭이 공유. 설정 탭은 "재시작 필요" 배지가 붙은 항목 옆에 버튼을
  바로 노출, System MCP 탭은 화이트리스트 토글/저장/커스텀 커맨드 추가·수정·삭제 중 하나라도
  하면 "⚠ System MCP 재시작" 버튼이 나타남.
- 의존성: `admin_console/backend/requirements.txt`에 `docker==7.1.0` 추가 → admin-console
  이미지 재빌드 필요(재시작만으로는 pip 패키지가 안 깔림), 소켓 마운트도 컨테이너 재생성
  (`docker compose up -d`)이 있어야 실제로 붙음.
- 보안 트레이드오프: 이 기능으로 admin-console 컨테이너가 호스트 Docker 데몬에 직접 접근하게
  됨(사실상 호스트 권한). 재시작 가능한 서비스는 코드에서 허용 목록으로 제한했지만,
  admin-console 자체가 공격당하면 호스트 전체가 위험해지므로 신뢰된 관리자망에서만 노출해야
  한다는 점을 사용자에게 명시적으로 전달함.

## 15. Manuals 탭 "제목과 파일을 먼저 선택하세요" 버그 수정 (완료)

파일 선택 자체는 정상 동작하고 있었고, 실제 원인은 title이 비어 있는 상태에서 같은 에러
메시지로 뭉뚱그려 보여준 것으로 추정됨(코드 확인상 file 바인딩 자체엔 문제 없음). 방어적으로
서버 파일/로컬 파일 선택 시 title이 비어 있으면 파일명(확장자 제외)으로 자동 채우도록 하고,
title/file 중 뭐가 빠졌는지 에러 메시지를 분리함.

## 16. VOC 엑셀 업로드 형식 자동 인식 (완료)

기존엔 4행 헤더(사내 표준 원본 포맷)만 지원해서 1행 헤더의 Question/Answer 단순 포맷 업로드가
`"지원하지 않는 형식입니다"`로 거부됨. 1행 헤더가 Question/Answer(대소문자 무관)면 이미 정제된
데이터로 보고 그대로 등록하고, 아니면 4행 헤더 포맷으로 시도, 둘 다 아니면 두 형식을 모두
안내하는 에러로 변경.

## 17. admin-console 빌드 실패 — `openpyxl` 사내 미러 간헐적 미탐지 (완료)

```
ERROR: Could not find a version that satisfies the requirement openpyxl==3.1.5 (from versions: none)
```
requirements.txt에 `bcrypt`/`docker`를 추가하면서 그 COPY 레이어가 바뀌어 `pip install -r
requirements.txt` 캐시가 처음으로 무효화됐고, 그 순간 사내 미러(Nexus)가 `openpyxl`을 못 찾는
문제가 처음 드러남(예전엔 이 레이어가 계속 캐시돼 있어서 안 보였을 뿐). `vendor/README.md`에
이미 문서화된 것과 같은 패턴(asyncpg가 간헐적으로 `from versions: none`을 주는 문제)이라,
같은 방식으로 오프라인 휠을 `vendor/`에 추가해 해결: `openpyxl-3.1.5`, `et_xmlfile-2.0.0`(의존),
`bcrypt-4.2.1`, `docker-7.1.0` + 의존성(`requests-2.34.2`, `urllib3-2.7.0`,
`charset_normalizer-3.4.9`). Dockerfile은 수정 없음(vendor의 모든 `*.whl`을 자동으로 먼저
오프라인 설치하는 기존 메커니즘 그대로 사용).

## 18. admin-console 빌드 실패 (2) — `python-pptx`도 같은 증상 (완료)

openpyxl을 vendor로 고치고 재빌드하니 requirements.txt의 바로 다음 줄인
`python-pptx==1.0.2`에서 동일하게 `from versions: none`. 사내 미러 결함이 한 패키지만이
아니라 파일 순서대로 여러 줄에 걸쳐 나타나는 것으로 확인됨. `python_pptx-1.0.2` + 의존성
`lxml-6.1.1`, `pillow-12.2.0`, `xlsxwriter-3.2.9`(`typing_extensions`는 기존 vendor 버전과
일치해 재사용)를 동일한 방식으로 vendor에 추가. requirements.txt의 나머지 줄(`redis`,
`bcrypt`, `docker`)은 이미 vendor에 있어 이어서 문제 없을 것으로 예상 — 재시도 결과 확인 필요.

## 19. 임베딩/리랭커 "CUDA busy" — 1차 진단(Exclusive_Process)은 틀렸음, 진짜 원인은 메모리 부족

LLM(`--max-model-len 32768`)은 정상 기동 확인됨. 이어서 임베딩(GPU 0)·리랭커(GPU 1)를 올리려니
"CUDA busy" 에러. 처음엔 `nvidia-smi`의 `Compute M.: E. Process`(Exclusive_Process)를 원인으로
의심해 `nvidia-smi -c 0`으로 Default 전환했으나, 재시도해도 똑같이 실패 — **Exclusive_Process는
원인이 아니었음.** 실제 로그로 확정:
```
ValueError: Free memory on device (7.38/79.21 GiB) on startup is less than desired GPU memory
utilization (0.15, 11.88 GiB). Decrease GPU memory utilization or reduce GPU memory used by other processes.
```
LLM의 실사용 메모리(약 73GiB)가 `--gpu-memory-utilization 0.85`의 이론치(약 67.3GiB)보다 커서
(CUDA 컨텍스트/드라이버 오버헤드 등) GPU당 실제 여유는 7.38GiB뿐인데, 임베딩이 요청한 0.15
(11.88GiB)가 이를 초과해서 난 순수 메모리 부족 에러였음("CUDA busy"는 사용자가 요약한 표현).
해결: 임베딩/리랭커의 `--gpu-memory-utilization`을 0.15 → 0.08로 낮춤.

## 21. 0.08로도 임베딩 OOM — 실제 원인은 좀비 프로세스가 아니라 구조적 공간 부족 (결정 완료)

메모리 정리 후 재시도해도 동일하게 실패 확인됨. 계산해보니 좀비 프로세스 문제가 아니라 원천적으로
자리가 없는 것으로 확정:
- Qwen3-235B-A22B-FP8을 TP=4로 돌리면 GPU 1장당 가중치만으로 약 59.3GiB 사용(235B×1바이트(FP8)
  ÷4장 ≈ 58.75GiB + 오버헤드). GPU 총 용량 79.21GiB 중 75%.
- `--gpu-memory-utilization`을 더 낮춰도 가중치만으로 이미 0.75를 넘게 써서, 최소 실행 가능한
  값이 약 0.77(KV 캐시 거의 0) — 지금 8GiB쯤 있는 KV 캐시 여유가 2~3GiB로 줄어 동시 요청
  처리량이 크게 떨어짐. LLM 쪽을 건드리는 건 실효성이 낮다고 판단.
- 대신 임베딩 모델을 Qwen3-Embedding-8B(80억 파라미터, GPU 자리 없음) → **BAAI/bge-m3**(5.68억
  파라미터)로 변경하기로 결정. 확인해보니 이 프로젝트의 DB 스키마가 모든 임베딩 컬럼을
  `vector(1024)`로 고정해뒀고(`shared/migrations.py`), `vllm_embed_model` 설정 기본값도 원래
  `"bge-m3"`였음(`shared/migrations.py:305`) — bge-m3의 기본 출력 차원이 정확히 1024라 스키마와
  맞는, 원래 설계된 조합이었음. Qwen3-Embedding-8B는 기본 출력 차원이 1024가 아니라서 그대로
  썼으면 메모리 문제와 별개로 "임베딩 차원 불일치" 에러로도 막혔을 것.
- 리랭커(bge-reranker-v2-m3)는 이미 충분히 작아 계획 변경 없음.
- 커맨드는 `docs/NEXT-STEPS.md` 1번 참고.

## 22. LLM/임베딩(bge-m3)/리랭커 전부 기동 확인 (완료)

`curl http://75.23.32.41:8000|8010|8020/v1/models` 전부 정상 응답 확인. hgpu4041 배포 완료.

## 23. 설정 탭 재구성 + 모델명 hot-reload 버그 수정 + Arena 모델 제거 (완료)

- 설정 탭: LLM/임베딩/리랭커 url+model+파라미터가 그룹별로 흩어져 있던 걸 각각 하나의 섹션으로
  재구성(`admin_console/frontend/index.html`의 `grouped` 정의).
- agent-server 버그: `/v1/models`·`/health`·채팅 응답의 `"model"` 필드가 컨테이너 기동 시점에
  고정된 `state["model_name"]`만 참조해서, `vllm_llm_model`이 hot_reload=true인데도 설정 탭에서
  바꾼 값이 Open WebUI 모델 목록에 반영 안 되는 버그였음(실제 채팅 라우팅은 `build_agent()`가
  매 요청 새로 설정을 읽어서 원래도 정상 동작 — 표시 이름만 stale했음). 매 요청
  `get_config()`로 새로 읽는 `_display_model_name()` 헬퍼로 교체.
- 같은 김에 mock-vllm이 아닌 실제 백엔드면 Open WebUI에 노출되는 모델명을 "AI Infra Assistant"로
  브랜딩(mock일 때는 기존처럼 실제 모델명 노출, 개발 중 구분 가능).
- open-webui에 `ENABLE_EVALUATION_ARENA_MODELS=false` 추가해 안 쓰는 Arena 비교 모델 제거.

## 24. Open WebUI 채팅 시 tool_choice 400 에러 — vLLM에 tool-calling 옵션 누락 (원인 확정)

```
litellm.BadRequestError: OpenAIException - Error code: 400 - {'error': {'message':
'"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set', ...}}
```
이 에이전트는 MCP 툴콜 기반이라 매 요청 `tool_choice: "auto"`를 보내는데, vLLM은 `--enable-
auto-tool-choice`와 `--tool-call-parser`를 명시적으로 켜야 이를 지원한다(기본은 거부). LLM을
그 옵션 없이 띄웠던 게 원인. Qwen3 계열의 표준 파서는 `hermes`(vLLM 공식 문서 확인) —
`docs/NEXT-STEPS.md` 1번에 옵션 추가한 `docker run` 커맨드 반영, 재기동 결과 확인 필요.
Sources: https://docs.vllm.ai/en/latest/features/tool_calling/ , https://qwen.readthedocs.io/en/latest/deployment/vllm.html

## 25. Open WebUI 기본 모델 미고정 (Open WebUI 쪽 설정, 코드로 강제 불가)

설정 저장 후 모델은 잘 뜨는데(`AI Infra Assistant`) 새 채팅에서 기본으로 선택되지 않음. 이건
agent-server/코드 문제가 아니라 Open WebUI의 기본 모델 선택 로직(브라우저별 마지막 선택 기억
또는 admin 설정)이라 관리자 패널 → 설정 → 모델에서 수동으로 기본 모델을 지정해야 함. mock ↔
실제 백엔드 전환 시 노출되는 모델 id 자체가 바뀌므로 그때마다 재지정 필요.

## 26. Open WebUI 기본 모델 자동 동기화 기능 (완료)

24번 후속으로 자동화 진행. Open WebUI 소스(v0.6.5) 확인 결과:
- `POST /api/v1/auths/signin`(email/password)으로 로그인하거나, 사용자별로 발급 가능한 API
  키(`GET/POST/DELETE /api/v1/auths/api_key`)를 그대로 `Authorization: Bearer` 로 admin 엔드포인트에
  쓸 수 있음 — 사람 비밀번호를 저장하지 않아도 되는 API 키 방식을 채택함.
- `POST /api/v1/configs/models`(`ModelsConfigForm`: `DEFAULT_MODELS`, `MODEL_ORDER_LIST`)가
  기본 모델을 설정하는 admin 전용 엔드포인트(`get_admin_user` 의존성).

구현:
- `shared/migrations.py`: `openwebui_base_url`(기본 `http://open-webui:8080`),
  `openwebui_admin_api_key`(secret) 설정 추가.
- `admin_console/backend/routers/ops.py`: `POST /api/ops/sync-openwebui-model` — agent-server
  `/v1/models`로 현재 노출 모델을 확인한 뒤 Open WebUI에 그 모델을 `DEFAULT_MODELS`로 지정.
  API 키 미설정 시 발급 방법을 안내하는 400 에러로 안전하게 생략.
- `admin_console/frontend/index.html`: 설정 탭에 "Open WebUI 연동" 그룹(API 키/주소 입력) +
  "LLM" 그룹에 "Open WebUI 기본 모델 동기화" 버튼 추가.
- 새 설정 키는 db-init을 다시 돌려야 심어짐 — `docs/NEXT-STEPS.md` 2번 참고.

Sources: https://github.com/open-webui/open-webui (v0.6.5 태그, backend/open_webui/routers/{auths,configs}.py)

## 27. 동기화 버튼 "405: Method Not Allowed" — admin-console 재시작 누락 (원인 확정)

새 라우트(`/api/ops/sync-openwebui-model`)를 추가했는데, admin-console을 재시작하라고 안내를
안 해서(agent-server/open-webui만 재시작하라고 했었음) 컨테이너가 옛 코드를 그대로 쓰고 있었음.
405는 FastAPI가 모르는 경로에 POST가 오면 `StaticFiles` 마운트가 대신 받아서 내는 기본 에러
(GET/HEAD만 지원) — 즉 그 라우트 자체가 없다는 신호였음. `docs/NEXT-STEPS.md` 1번에 admin-console
재시작을 포함하도록 커맨드 수정.

## 28. dev-config가 관리자 지정 vLLM 설정을 mock으로 되돌리는 문제 (완료)

`docker-compose.dev.yml`의 `dev-config`가 `vllm_llm_base_url` 등을 무조건 UPDATE로 mock 값 덮어써서,
`down && up` 등으로 서비스가 재기동될 때마다 관리자가 설정 탭에서 저장한 값이 사라졌음(이전에
"컨테이너를 살려둔 채로만 설정 변경"이라고 우회 안내했던 근본 원인). `config_seed()`가 최초
시딩 시 `updated_by='bootstrap'`으로 넣고, 설정 탭 저장 시엔 관리자 계정명으로 바뀌는 것을
이용해 각 UPDATE에 `AND updated_by='bootstrap'` 조건을 추가 — 한 번이라도 손댄 값은 이후
dev-config가 다시 실행돼도 안 건드림.

## 29. 채팅 응답이 스트리밍 안 되고 한 번에 나오는 문제 (완료)

`agent_server/main.py`의 SSE 스트리밍 핸들러가 `runner.run_async()`를 `run_config` 없이
호출하고 있었음 — ADK의 기본 `RunConfig.streaming_mode`는 `NONE`이라, LLM 응답을 다 모은 뒤
이벤트 1~2개로만 내보낸다(SSE로 델타를 보내는 코드 자체는 맞았지만, 애초에 델타 단위 이벤트가
안 왔던 것). `google.adk.agents.run_config.RunConfig(streaming_mode=StreamingMode.SSE)`를
3개 스트리밍 엔드포인트(`/v1/chat/completions`, `/v1/agent/query`, `/v1/voc/query`)의
`event_stream()` 내부 `run_async` 호출에 추가. 논스트리밍 분기(최종 응답만 쓰는 코드)는
그대로 둠. `pip install google-adk==1.22.1`로 로컬에서 `RunConfig`/`StreamingMode`/
`Runner.run_async` 시그니처 직접 확인 후 작업(추측 아님).

## 30. 동기화 버튼 405 재발 — admin-console 실제 재시작 여부 확인 필요 (조치 중)

이전 라운드에서 admin-console 재시작을 안내했는데도 같은 405가 반복됨. `docs/NEXT-STEPS.md`
2번에 `docker compose ps`/`logs`/직접 curl로 admin-console이 정말 새 코드로 떴는지 확인하는
커맨드 추가 — 재현되면 로그를 받아서 진단 이어감.

## 31. mock 전환 테스트 시 `mock-llm`이라는 잘못된 호스트명 사용 (사용자 확인 필요)

`vllm_llm_base_url`을 `http://mock-llm:8000`으로 바꿔 테스트했는데, 실제 mock 컨테이너 이름은
`mock-vllm`이라 그 호스트가 존재하지 않음 — 오타로 보임. 브랜딩 로직(`MOCK_LLM_BASE_MARKER =
"mock-vllm"`)이 그대로인지, 아니면 넓혀야 하는지는 실제 정상 케이스(올바른 호스트명)로 재확인
필요.

## 32. 매뉴얼 등록 후 RAG 검색 안 됨 — 발행(publish) 여부 확인 필요 (해결됨)

"슈퍼컴 계정 신청 방법"을 못 찾음. `mcp_servers/manual_mcp/server.py`의 검색 쿼리가 전부
`status='published'`만 대상으로 하므로(초안은 검토 전이라 의도적으로 제외), 등록만 하고
발행을 안 했으면 검색에 안 잡히는 게 정상 동작. 발행 후 정상적으로 매뉴얼('supercom_guide_260727')
기반 답변이 나오는 것 확인됨(홈 스토리지 할당량 질문에 매뉴얼 내용 인용해서 답변).

## 33. System MCP `disk_free`가 "비활성화" 응답 — 실제 원인은 dev에 ssh 인프라가 없었던 것 (완료)

admin_console 화면엔 `disk_free`가 enabled로 보이는데, 실제 질문하면 에이전트가
"'disk_free' 툴 비활성화"라고 답함. `shared/mcp_caller.py`의 `PermissionError` 메시지와
문구가 비슷해서 처음엔 enabled 상태 불일치로 의심했으나, **실행 로그 자체가 하나도 없다는
사실**로 원인이 바뀜 — 로그가 없다는 건 LLM이 애초에 툴 호출을 시도조차 안 했다는 뜻(호출됐다면
성공/차단/에러 어느 경우든 `job_logs`에 남는다). 확인해보니 `docker-compose.dev.yml`의
command-mcp/system-mcp에는 `docker-compose.yml`(prod)에 있던 `HOSTS_FILE`/`SSH_KEY` 환경변수와
`/etc/hosts`·ssh 개인키 마운트가 **아예 없었음** — dev 환경 자체가 ssh 실행을 위한 인프라를
갖추지 못한 상태였고(LLM이 사용할 host 이름도 몰라서 애초에 호출을 안 한 것으로 보임), enabled
플래그와는 무관한 문제였음. prod와 동일한 방식으로 dev compose에 배선함(`docs/NEXT-STEPS.md`
1~4번). 로그인 서버는 `login07`(202.20.185.10)로 확정, 호스트 `/etc/hosts` 등록 + ssh 개인키
준비가 추가로 필요함(사용자가 직접 수행).

## 35. `login07`의 실제 IP는 202.20.185.10이 아니라 게이트 서버 202.20.185.100 (정정)

202.20.185.10은 배포 호스트(202.20.183.30)에서 직접 안 닿고, 202.20.185.100이 "로그인 서버로
자동 라우팅하는 게이트 서버"임을 확인함(202.20.185.100 자체의 `/etc/hosts`에 내부 서버들이
등록돼 있어 거기로 ssh하면 알아서 연결됨). `/etc/hosts`의 `login07` 매핑을 202.20.185.100으로
정정 — 코드(`ssh_exec.py`)는 변경 없이, `resolve_host()`가 게이트 IP로만 풀면 되고 실제
내부 라우팅은 게이트 서버 쪽에서 처리됨. `ssh-copy-id`로 개인키를 202.20.185.100에 등록해야
비밀번호 프롬프트 없이 접속됨(개인키를 로컬에 두는 것만으로는 인증이 안 됨 — 상대측
`authorized_keys`에 공개키 등록이 필요).

## 36. ssh 키 등록 확인 완료 + `scheduler_login_host`가 설정 탭에 안 보이던 버그 (완료)

`ssh-copy-id` 후 `ssh root@202.20.185.100 whoami`가 비밀번호 없이 `root`를 반환 — ssh 인프라
준비 완료 확인. 이어서 설정 탭에서 `scheduler_login_host`를 찾을 수 없다는 리포트 — 원인은
`admin_console/frontend/index.html`의 `grouped` 필터 목록 어디에도 이 키가 안 걸려서(LLM/임베딩/
리랭커/검색품질/MCP엔드포인트/OpenWebUI연동/에이전트지시문 중 매칭 없음) "기타" 그룹에만
묻혀 있었던 것(실제로는 존재했음, `env_managed`로 숨겨진 것도 아니었음). "SSH 실행
(System/Command MCP)" 전용 그룹으로 분리해 눈에 띄게 고침.

또한 "커맨드는 절대 root 권한으로 실행되면 안 된다"는 요구사항이 기존 코드로 이미 100% 보장됨을
재확인: `shared/ssh_exec.run_ssh_as_user()`가 System/Command MCP의 모든 실행 경로에서 유일하게
쓰이고, 매번 `su - <user_id> -c '...'`로 감싸 실행한다(우회 경로 없음, ssh 자체만 root).

## 37. System MCP 툴 "분류"(host_mode) 기능 추가 (완료)

요청: list_dir/find_files/read_file_head처럼 서버별로 결과가 다르지 않은 툴은 host를 안 물어보고
로그인 서버로 고정 실행, gpu_status/disk_usage/system_info처럼 서버마다 다른 툴은 LLM이 서버명을
지정하도록 관리자 콘솔에서 "분류"로 지정할 수 있게 해달라는 요청.

- `shared/mcp_caller.py`의 `build_wrapped()`에 `host_mode`/`login_host` 파라미터 추가 —
  `user_scoped`의 `scope_param` 숨김·강제주입과 완전히 같은 메커니즘을 host에도 적용
  (`host_mode="login_server"`면 host를 LLM 스키마에서 제거하고 `login_host()`가 돌려주는 값을
  강제 주입). `load_overrides_sync()`는 `extra_columns`로 일반화해서 Command MCP 쪽 동작은
  그대로 유지.
- `system_whitelist_state`/`system_custom_commands`에 `host_mode` 컬럼 추가(마이그레이션
  system_db v6, CHECK 제약 target_server|login_server).
- 코드 내장 화이트리스트: list_dir/find_files/read_file_head → login_server, gpu_status/
  disk_free/disk_usage/system_info → target_server. disk_free는 예외로 "개인 홈 스토리지
  질문이면 로그인 서버로" 문구를 설명에 추가(target_server 유지 — 서버 특정 질문과 개인 계정
  저장공간 질문을 구분해야 해서 스키마 레벨로 자동화하지 않고 프롬프트 레벨로 처리).
- admin_console 설정 탭 System MCP 카드/커스텀 커맨드 폼에 "분류" 드롭다운 추가(변경 시 System
  MCP 재시작 필요 — description_override와 동일한 제약, 스키마에 영향을 주기 때문).
- Command MCP는 애초에 host 파라미터가 없어서(항상 로그인 서버 기준) 이 기능이 필요 없음 —
  사용자 질문에 답변: "실행은 System MCP 담당"은 System MCP의 서버 점검 툴 섹션을 가리킨 것이고,
  Command MCP도 `get_scheduler_job_info`(본인 job 조회)는 실제로 ssh 실행한다.

## 38. 에이전트 지시문 SOTA 프롬프트로 재작성 (완료, 콘솔에서 직접 붙여넣기 필요)

기존 지시문에 실제로 존재하지 않는 `command.get_scheduler_queue_status` 툴이 언급돼 있었음
(command_mcp/server.py의 EXEC_WHITELIST에 없음 — 발견해서 제거). 최우선 원칙/답변 전
체크리스트/도구 라우팅(로그인서버 고정형 vs 서버지정형 명확히 구분)/답변 형식/최종 재확인
구조로 재작성. `shared/migrations.py`의 `AGENT_INSTRUCTION` 기본값을 갱신했지만
`agent_system_instruction`은 non-force 시드라 기존 배포 DB에는 자동 반영 안 됨 — 설정 탭에
직접 붙여넣어야 함(`docs/NEXT-STEPS.md` 3번에 전체 텍스트 있음).

로그인 서버 이름(`login07`)을 지시문 텍스트에 하드코딩하지 않고 `agent_server/agent.py`의
`build_agent()`가 매 요청 `scheduler_login_host`를 읽어 지시문 끝에 동적으로 붙이도록 함 —
나중에 로그인 서버가 바뀌어도 지시문을 다시 편집할 필요 없음.

## 39. Open WebUI dev에서 일반 사용자 계정 테스트 불가 (완료)

`WEBUI_AUTH=false`라 로그아웃해도 항상 같은 계정으로 자동 로그인돼 admin 외 일반 계정을 만들 수
없었음. `docker-compose.dev.yml`에서 `WEBUI_AUTH=true`로 전환하고, 역할 판단에 필수인
`ENABLE_FORWARD_USER_INFO_HEADERS=true`(이게 없으면 "필요 역할" 검사가 항상 실패함 — 이전엔
dev에 아예 없었음)를 추가. 계정/대화 데이터가 컨테이너 재생성 시 사라지지 않도록 영구 볼륨
(`open_webui_dev_data`)도 추가함(prod와 동일 패턴, 이전엔 dev에 볼륨이 없어 매번 초기화됐음).

## 40. Open WebUI 관리자 계정 이메일 변경 불가 (Open WebUI 자체 제약, 확인됨)

Open WebUI 업스트림의 알려진 제약 — "슈퍼 admin"(최초 계정)은 UI에서 본인 이메일을 못 바꿈
(다른 사용자 계정 이메일은 관리자 패널에서 바꿀 수 있는데 본인 것만 예외, 업스트림에도 미해결
이슈로 등록돼 있음). 우회: (1) 계정을 하나 더 만들어 그 계정으로 관리자 패널에서 첫 계정 이메일
수정, 또는 (2) `docker compose exec open-webui sqlite3 /app/backend/data/webui.db "UPDATE user
SET email=... WHERE email=...;"`로 DB 직접 수정.
Sources: https://github.com/open-webui/open-webui/issues/14986

## 41. "AI Infra Assistant"가 모델 목록에서 안 보임 (진단 중)

admin_console 설정 탭 URL은 정상인데(vllm_llm_base_url — agent-server→vLLM 연결과 무관),
Open WebUI 모델 목록에 안 뜬다는 리포트. WEBUI_AUTH를 켜고 영구 볼륨으로 바꾸면서(#39) Open
WebUI의 내부 DB가 완전히 새로 시작됐을 가능성이 있음 — 이전에 저장돼 있던 Connections 설정이
날아갔을 수 있음(env var `OPENAI_API_BASE_URL`은 여전히 유효해야 하지만, 재확인 필요).
`docs/NEXT-STEPS.md` 1번에 agent-server 직접 curl 확인 → open-webui 로그 확인 → Open WebUI
관리자 패널의 Connections/Models 설정 확인까지 진단 순서를 정리함.

curl은 정상(`"AI Infra Assistant"` 응답), open-webui 로그엔 `host.docker.internal:11434`
(Ollama 기본 포트) 연결 실패만 있음 — 이건 Open WebUI가 기본으로 시도하는 Ollama 연결이라
우리 설정과 무관, 무시해도 됨. **로그에 agent-server(OpenAI 연결) 시도 자체가 안 보이는 게
단서** — WEBUI_AUTH를 켜고 새 영구 볼륨으로 전환하면서(#39) Open WebUI의 "연결(Connections)"
설정이 비어 있을 가능성이 큼(env var는 살아있어야 하지만 컨테이너 안에서 직접 확인 필요).
`docs/NEXT-STEPS.md` 1번에 `env | grep OPENAI` 확인 커맨드 + Admin Panel Connections/Models
수동 확인·추가 절차 정리함.

**해결 확인**: Open WebUI Connections에 OpenAI API 키를 등록 안 해서(연결 자체는 있었지만 키
필드가 비어 있었던 듯) 모델 목록이 안 뜬 것 — 키를 등록하니 "AI Infra Assistant" 정상 표시됨.

## 42. sqlite3 CLI가 open-webui 이미지에 없음 (완료)

이메일 변경 커맨드로 안내한 `sqlite3` 바이너리가 이미지에 없어서 실패
(`exec: "sqlite3": executable file not found in $PATH`). 이미 들어있는 python3 표준 라이브러리
`sqlite3` 모듈로 대체(`docs/NEXT-STEPS.md` 1번) — 별도 패키지 설치 없이 동일 작업 가능.

## 43. API 키 등록 후에도 admin만 모델이 보이고 채팅 시 "Model not found", 일반 사용자는 모델 자체가 안 보임 (조치 중)

API 키를 Open WebUI 관리자 패널(Admin Panel → Settings → Connections, 인스턴스 전체 적용)이
아니라 개인 계정 설정(우측 상단 프로필 → 설정 → 연결, "Direct Connections" — 그 계정에서만
보이는 개인용 연결)에 등록했을 가능성이 높다고 보고 안내함. "admin이 키 하나만 등록하면 전체
사용자에게 default로 떠야 한다"는 요구사항과도 정확히 일치하는 설명(개인 연결은 원천적으로
그 계정에만 적용되는 게 Open WebUI의 정상 동작이라 그쪽에 등록했다면 여러 사용자에게 안 보이는
게 당연함). `docs/NEXT-STEPS.md` 2번에 관리자 패널 경로로 다시 등록하는 절차 안내 — 그래도
안 되면 브라우저 콘솔 로그 요청함.

**정정(중요)**: 사용자가 실제로 한 것은 Open WebUI 개인 계정의 API 키(설정 → 계정 → API 키)를
admin_console의 "Open WebUI 연동" 설정(`openwebui_admin_api_key`)에 넣은 것 — 이건 "Open WebUI
기본 모델 동기화" 버튼(#26)용이고, 모델이 보이고 채팅되게 하는 것과는 전혀 무관한 별개 기능임.
실제로 필요한 건 Open WebUI 자체의 관리자 패널 → 설정 → **연결(Connections)**에
`http://agent-server:8000/v1`을 OpenAI API 커넥션으로 등록하는 것(#41에서 이미 안내했던 것과
동일) — 두 화면이 이름이 비슷해 혼동한 것으로 보임. `docs/NEXT-STEPS.md`에 두 기능을 표로
명확히 구분해서 정리함.

## 44-보충. Connections 등록 후에도 일반 사용자에게 모델 자체가 안 보임 (원인 확정, 조치 안내)

Connections(#41/#43)는 정상 등록했는데도 "user" 계정에 모델이 전혀 안 보이고 default도 없음.
이건 Connections와 별개로 **Open WebUI 각 모델 항목의 "공개 범위(Visibility)"** 설정 때문 —
Open WebUI 커뮤니티에 다수 보고된 알려진 동작으로, 모델을 admin/특정 그룹에만 보이게 기본
설정해두고 명시적으로 "Public"으로 바꿔야 일반 사용자에게 보인다. Admin Panel → Settings →
Models에서 해당 모델을 Public으로 바꾸도록 안내함(`docs/NEXT-STEPS.md` 1번).
Sources: https://github.com/open-webui/open-webui/discussions/9058 , https://github.com/open-webui/open-webui/discussions/4468 , https://docs.openwebui.com/features/authentication-access/rbac/permissions/

## 45. System MCP "disabled by admin" 재발 — db-init 미실행 의심 (조치 안내)

host_mode 컬럼 추가(#37) 이후 admin_console의 PATCH /whitelist가 `host_mode`도 같이 조회하도록
바뀌었는데, 그 컬럼이 없는 채로(=db-init 재실행 없이 코드만 최신화) 콘솔에서 스위치를 켜면 PATCH
자체가 서버 에러로 실패해서 겉보기엔 "켰는데 다시 꺼진 것처럼" 보일 수 있음(enabled 토글이 실제로
DB에 반영이 안 됨). `docs/NEXT-STEPS.md` 1번에 db-init 재실행 확인 절차 안내 — 재현되면
db-init 로그도 받아서 마이그레이션 자체 실패 여부 확인 예정.

## 46. 카탈로그에만 있는 커맨드도 실행 가능하게 해달라는 요청 — 안전 설계 확인 요청 (진행 중)

"Command/System MCP에 등록 안 된 커맨드도 카탈로그(매뉴얼 db)에서 찾아서 실행"이라는 요청.
현재 커맨드 카탈로그(`command_catalog`, Command MCP `search_commands`)는 순수 조회용 메타데이터
(이름/설명/사용법 텍스트)라 실행 가능한 argv가 없음 — 이걸 그대로 실행하면 검증 안 된 텍스트를
셸/argv로 실행하는 셈이라 위험(엑셀 대량 업로드 데이터라 사람이 한 줄씩 검토 안 했을 수 있음).
System MCP에 이미 있는 "커스텀 커맨드"(argv_template + `shared/custom_commands.py` 검증) 방식을
Command MCP에도 동일하게 추가하는 안전한 방식을 제안하고 사용자 확인 요청함 — 승인되면 구현.

## 44. Open WebUI 데이터(계정 DB) 초기화 요청 (완료, 커맨드 제공)

이메일 DB를 이상하게 만든 뒤 아예 초기화 요청. `open_webui_dev_data` 명명 볼륨만 지우면
postgres(플랫폼 데이터)는 안 건드리고 Open WebUI 계정/대화만 초기화됨 — `docker compose down
-v`(전체 볼륨 삭제)는 명시적으로 쓰지 말라고 경고함. 초기화 후 회원가입 + 연결(Connections)
재등록 절차를 NEXT-STEPS에 안내.

## 34. 에이전트가 "슈퍼컴" 관련 질문에 호스트를 안 밝히면 되묻기만 함 (커맨드 안내함)

`disk_free(user_id, host)`가 host를 필수로 받는데, LLM이 실제 로그인 서버 이름을 모르니
"어떤 시스템 기준이냐"고 되묻는다. `scheduler_login_host`(Command MCP 전용, disk_free와는
별개 설정)와 별개로, `agent_system_instruction`에 로그인 서버 이름(`login07`)과 "되묻지 말고
바로 호출하라"는 문장을 추가해야 함 — 정확한 문구는 `docs/NEXT-STEPS.md` 5번 참고(설정 탭에서
직접 저장 후 agent-server 재시작 필요, hot_reload=false 키라서).

## 20. 매뉴얼 엑셀 열 선택 UI 이해 어려움 (완료)

"내용/제목/페이지 체크박스가 뭔지 모르겠다"는 피드백에 번호 매긴 단계별 설명과, 현재 선택으로
첫 번째 행이 실제로 어떻게 저장될지 실시간으로 보여주는 미리보기 박스를 추가함.

## 47. 카탈로그 커맨드 전건 실행 허용 (완료) — Command MCP 화이트리스트 폐지

#46의 안전 설계 질의에 대한 결정: **"Command MCP는 모두 실행 가능한 걸로 업로드한다. 매뉴얼
db에서 찾은 커맨드도 모두 실행 가능해야 한다. 화이트리스트 관리는 오직 System MCP만."**
제안했던 "카탈로그 항목별 실행 등록(argv_template) 버튼" 방식은 채택하지 않고, 카탈로그 등록
자체를 승인으로 간주하는 방식으로 구현함.

- `command_catalog`에 `exec_command` 열 추가(마이그레이션 command_db v5). 실제 실행할 커맨드
  문자열이며, **비어 있으면 name을 그대로 실행**한다 — 기존에 올려둔 카탈로그도 추가 작업 없이
  바로 실행 가능하다는 뜻(사용자 요구사항의 핵심). `{user_id}` 토큰은 호출자 계정으로 치환.
- Command MCP에 `run_command(name, args?, host?)` 툴 추가. 카탈로그에서 이름으로 찾아
  `shared/catalog_exec.build_catalog_argv()`로 argv를 만들고 기존 `run_ssh_as_user()`로 실행한다
  (실행 경로는 System MCP와 동일 — ssh root → `su - <user_id>`). host 미지정 시 로그인 서버.
  user_scoped=True라 user_id는 LLM 스키마에서 숨겨지고 호출자 신원에서 강제 주입된다.
- Command MCP의 enabled/required_roles 검사를 제거(`_always_enabled`/`_no_required_roles`).
  `EXEC_WHITELIST` → `EXEC_TOOLS`로 이름도 바꿔 "여긴 화이트리스트가 아니다"를 코드에 반영.
  감사로그(job_logs)는 그대로 전건 기록. System MCP는 기존 화이트리스트 관리 그대로 유지.
- 화이트리스트가 없어진 자리를 구조적 안전장치로 대체: (1) 항상 본인 권한 강등 실행,
  (2) 셸 미사용 argv 실행(인젝션 불가, 대신 파이프/리다이렉션은 명시적 에러로 안내),
  (3) /etc/hosts 등록 서버만, 타임아웃·출력 상한, (4) 파괴적 기본 명령 실행 시점 거부
  (설정 `catalog_exec_deny_commands`, 기본값은 System MCP 커스텀 커맨드와 동일 목록 — 비우면
  제한 없음), (5) 전건 감사로그. 엑셀 대량 업로드본이라 사람이 한 줄씩 검토 안 했을 수 있다는
  #46의 위험은 (4)로만 남겨두고 실행 자체는 막지 않음.
- 관리자 콘솔 커맨드 탭: "실행 커맨드" 입력칸 + 목록 열 + 엑셀 열 매핑(exec_command_column)
  추가. 실행 커맨드 열을 매핑하지 않고 다시 업로드해도 기존 값이 지워지지 않게 upsert에
  `COALESCE`를 넣음. 탭 설명도 "정보 제공용, 실행은 System MCP 담당" → "조회 및 실행"으로 정정.
- 에이전트 지시문에 "커맨드 카탈로그 실행" 라우팅 추가 — 사용자가 상태 확인을 요청하면 사용법만
  안내하지 말고 search_commands로 찾아 run_command로 실행해 결과로 답하도록 명시.
  `agent_system_instruction`은 non-force 시드라 기존 배포 DB에 자동 반영되지 않음 →
  `docs/NEXT-STEPS.md` 2번에 전문을 넣어두었으니 설정 탭에 붙여넣어야 함.
- 확인: `run_command`의 MCP 스키마에 user_id가 노출되지 않고 name/args/host만 노출되는 것,
  argv 빌더가 rm·/bin/rm·파이프·제어문자를 거부하고 `{user_id}` 치환이 되는 것을 실제로 실행해
  검증함.

주의: 새 툴이 에이전트에 보이려면 db-init(command_db v5) 후 command-mcp와 agent-server를
재시작해야 한다.

## 48. 콘솔 설정(vLLM 주소 등)이 통째로 초기화됨 — dev-config의 mock 되돌리기 (조치)

증상: `vllm_llm_base_url` 등 설정 탭 값이 전부 초기값/mock으로 돌아감.

원인: `docker-compose.dev.yml`의 `dev-config` 서비스가 `docker compose up` 할 때마다
vLLM 주소/모델명·`rerank_base_url`·`redis_url`을 mock 값으로 UPDATE한다. #28에서
`AND updated_by='bootstrap'` 가드를 넣어 "콘솔에서 저장한 값"은 지켜지게 했지만, 그 가드는
**platform_config DB가 새로 만들어지는 경우를 못 막는다** — 시드 직후에는 모든 행이
`updated_by='bootstrap'`이라, `.env`에 실제 vLLM 주소를 넣어뒀더라도 시드되자마자 mock으로
덮인다(설정이 통째로 초기화된 것처럼 보임). `db-init` 자체는 force=False 키의 value를 건드리지
않으므로 범인이 아님.

조치: mock 되돌리기 조건을 이중으로 바꿈 — `updated_by='bootstrap'` **그리고**
`vllm_llm_base_url`이 아직 `CHANGE-ME` 자리표시자일 때만 mock으로 덮는다(모델명/리랭커는
base_url이 실제로 mock일 때만). `.env`에 실제 주소가 들어간 실서버는 DB를 새로 만들어도
mock으로 안 돌아간다. `redis_url`만 예외로 항상 비운다(dev compose에 redis 컨테이너가 없음).

복구값은 `CLAUDE.md` 2-2절과 `docs/NEXT-STEPS.md` 0번에 표로 기록함(RUN-LOG #12/#21/#22 기준:
LLM `qwen3-235b-a22b` :8000, 임베딩 `bge-m3` :8010, 리랭커 `bge-reranker-v2-m3` :8020,
로그인 서버 `login07`). 실제 서빙 이름은 `/v1/models` curl로 확인하도록 안내.

주의: postgres 볼륨이 삭제되면(`docker compose down -v`) 설정뿐 아니라 매뉴얼/VOC/커맨드
카탈로그까지 전부 사라진다 — #44에서 경고했던 그대로, `down -v`는 쓰지 않는다.

## 49. 카탈로그 필드 정리(이름/실행 커맨드/설명) + 지시문에서 System MCP 툴 나열 제거 (완료)

요청 두 건.

**(1) 커맨드 카탈로그 탭 필드 정리** — "이름/커맨드/설명만 있으면 되지 않나. 카테고리 '-',
검색 '키워드만'은 뭐냐, 필요 없으면 삭제해."
- `usage`(사용법)·`category`(카테고리)를 콘솔 폼·목록·엑셀 열 매핑·API·MCP 툴에서 전부 제거.
  `search_commands`의 `category` 파라미터도 삭제(LLM 스키마도 단순해짐). 실행 커맨드가 있으면
  사용법 텍스트는 실질적으로 불필요해서 정보 손실이 없다.
- 검색 열의 "의미+키워드 / 키워드만"은 **그 항목에 임베딩이 있는지** 표시였다(임베딩이 없으면
  키워드가 정확히 맞아야만 검색됨 = 등록 시점에 임베딩 서버가 응답 안 했다는 뜻). 열 자체는
  없애되, 임베딩 없는 항목이 있을 때만 상단에 경고 배너로 알리도록 바꿈 — 평소엔 안 보이고,
  실제 문제가 있을 때만 원인과 조치(설정 확인 후 편집·저장하면 재생성)를 안내한다.
  #48의 설정 초기화 때 등록한 커맨드라면 이 배너가 떠 있을 것.
- DB 컬럼(`usage`/`category`)은 DROP하지 않고 남겨둠 — 이미 엑셀로 올린 값이 있을 수 있어
  되돌릴 수 없는 데이터 삭제는 피했다. 코드 경로에서는 더 이상 읽지 않는다.
- `search_commands`/`get_command_detail` 결과는 `command` 필드로 **실제 실행될 커맨드**를 준다
  (`exec_command`가 비면 name). LLM이 "무엇이 실행되는지"를 항상 정확히 보게 된다.

**(2) 에이전트 지시문에서 System MCP 툴 이름 나열 제거** — "나열하면 나중에 커맨드 추가됐을 때
어떻게 하려고."
- 기존 지시문은 `list_dir/find_files/read_file_head`(로그인 서버 고정형),
  `gpu_status/disk_usage/system_info`(서버 지정형), `disk_free` 예외를 하드코딩하고 있었다 —
  콘솔에서 툴을 추가하거나 분류를 바꿀 때마다 지시문을 고쳐야 하는 구조.
- `host_mode` 기능(#37)이 **로그인 서버 고정형 툴에서는 host 파라미터 자체를 LLM 스키마에서
  제거**한다는 점을 이용해, 툴 이름 대신 **"host 파라미터가 있는지"로 판단**하도록 일반화함.
  host가 없으면 그냥 호출, 있으면 (1) 사용자가 밝힌 서버 → (2) 특정 서버가 아니라 본인 계정/홈
  기준 질문이면 로그인 서버 → (3) 그 외엔 되묻기. `disk_free` 예외도 이 규칙에 자연히 흡수됨.
- 이제 콘솔에서 커맨드를 추가/분류 변경해도 지시문을 다시 손댈 필요가 없다.

## 50. 홈 스토리지 용량 질문 실패 — 계정명 정규식이 점(.)을 막고 있었음 (완료)

두 계정으로 같은 질문을 했는데 둘 다 실패한 리포트.

**사례 2 (`yr9.choi` 계정) — 진짜 버그.** "계정 형식 관련 문제"라는 답변은
`shared/ssh_exec.validate_user()`의 `PermissionError`가 그대로 올라온 것.
정규식이 `^[a-z_][a-z0-9_-]{0,63}$`라 **점(.)을 허용하지 않아서** 사내 계정 `yr9.choi`가
통째로 거부되고 있었다(ssh 실행 자체가 시작도 안 됨). `.`을 허용하도록 수정
(`^[a-z_][a-z0-9_.-]{0,63}$`). 첫 글자를 `[a-z_]`로 고정한 건 유지 — `-`로 시작하는 이름이
ssh/su 옵션으로 해석되는 걸 막기 위함이라 안전성은 그대로다.

**사례 1 (관리자 계정) — 원인이 다름.** Open WebUI 이메일의 로컬파트가 그대로 리눅스 계정으로
쓰이는데(`agent_server/main.py:_to_os_identity`), 그게 서버에 없는 계정이면 `su - <user>`가
실패한다. 그런데 에이전트는 이걸 "권한이 없어 거부됨"으로 답해버렸다(su 실패라 커맨드는
실행되지도 않았는데). `run_ssh_as_user()`가 stderr에서 su의 "계정 없음" 패턴을 감지하면
`error` 필드에 "서버에 그 계정이 없어서 실패(권한 문제 아님) + 이메일 @앞부분이 서버 계정명과
같아야 함"을 명시하도록 함.

**추가로 발견한 위험 — 관리자 이메일이 `root@...`면 root로 실행된다.** agent 컨테이너는 root로
ssh하고 `su - <user_id>`로 강등하는데, user_id가 `root`로 들어오면 강등이 무의미해져
"어떤 경우에도 root로 실행하지 않는다"는 절대 규칙이 깨진다. `DENY_USERS = {root, toor}`를
추가해 실행 단계에서 거부한다(사례 1이 관리자 계정이었던 걸 감안하면 실제로 root로 df가
돌았을 가능성이 있었음).

부수 수정: `_to_os_identity()`가 소문자로 정규화한다 — Open WebUI 이메일에 대문자가 섞여 있어도
리눅스 계정(소문자)에 매핑되도록.

반영: `shared`가 마운트라 `bash scripts/restart-mounted.sh`만으로 반영된다(재빌드 불필요).

## 51. disk_free가 홈 스토리지 질문에 잘못 라우팅 + 매뉴얼 CSV 업로드 지원 (완료)

**(1) 홈 스토리지 용량 = df가 아니다.** `disk_free`(df -h)는 서버 전체 파일시스템 용량이라
개인 계정 할당량과 무관한데, 지시문과 툴 설명이 "개인 홈 스토리지 할당량처럼 특정 서버를 안 밝힌
질문이면 로그인 서버 이름으로 조회한다"고 적혀 있어 에이전트를 df로 유도하고 있었다(#37에서
프롬프트 레벨로 처리하려던 게 오히려 잘못된 라우팅을 만든 것).
- `whitelist.py`의 disk_free 설명: "**개인 계정 할당량 질문에는 쓰지 않는다**, 그런 질문은 커맨드
  카탈로그에서 찾아 run_command로 실행한다"로 교체.
- 지시문: 커맨드 카탈로그 섹션을 "**개인 계정 관련 질문의 1순위**"로 승격하고, System MCP 섹션
  끝에 "개인 계정 할당량/사용량은 이 도구들이 아니라 run_command로 처리한다"를 명시.
- 카탈로그에 `myquota`가 등록돼 있어야 동작한다(NEXT-STEPS 6번).

**(2) 매뉴얼 탭 CSV 업로드 불가.** `전처리 실패: 지원하지 않는 형식입니다. 지원: .docx, .md,
.pdf, .pptx, .txt` — 프론트가 `.xlsx/.xls`만 "열 선택" 경로로 보내고 나머지는 전부 문서 파서로
보내서, `.csv`가 문서 파서에 걸려 거부됐다(백엔드에 CSV 경로 자체가 없었음).
- `spreadsheet.py`를 "표 형식 파일" 공통 모듈로 확장: `read_table_meta`/`load_table_rows`가
  확장자를 보고 openpyxl 또는 csv로 분기한다. CSV는 인코딩(UTF-8 BOM/CP949/UTF-8/latin-1)과
  구분자(`,` `;` tab `|`)를 자동 판별하고, 헤더보다 짧은 행은 길이를 맞춰 인덱스 에러를 막는다.
  (사내 엑셀에서 저장한 CSV가 CP949인 경우가 많아 실제로 두 인코딩 모두 테스트함.)
- 매뉴얼/커맨드 카탈로그 두 라우터의 허용 확장자를 `TABLE_EXTS`(xlsx/xls/csv)로 통일.
- 프론트: `.csv`를 열 선택 경로로 라우팅, 파일 선택 accept와 안내 문구에 CSV 추가.

반영: `shared`/`admin_console` 모두 마운트라 `bash scripts/restart-mounted.sh`로 반영된다.

## 52. 매뉴얼에서 찾은 커맨드도 등록 없이 실행 — run_command를 출처 무관으로 (완료)

정정된 요구사항: "myquota를 Command MCP에 지정하는 게 아니라, 사용자 요청이 오면 **매뉴얼 db /
커맨드 카탈로그 / System MCP 화이트리스트 세 곳에서** 적절한 커맨드를 찾아서 실행해야 한다."

#47에서 만든 `run_command(name=...)`는 카탈로그에 **등록된 이름만** 실행할 수 있었다
(없으면 "카탈로그에 없다"고 거부). 그래서 매뉴얼 문서에만 적혀 있는 커맨드는 관리자가 카탈로그에
손으로 등록해야 실행됐고, 이건 #46의 원래 요구("등록 안 된 커맨드여도 매뉴얼 db에서 검색해서
실행")를 절반만 만족한 것이었다.

- 파라미터를 `name` → `command`로 바꾸고, **카탈로그 조회는 있으면 쓰고 없으면 받은 문자열을
  그대로 커맨드로 본다.** 카탈로그에 있으면 exec_command로 치환(등록된 실행 커맨드가 우선),
  없으면 매뉴얼에서 뽑은 커맨드를 그대로 실행. 결과에 `source`("카탈로그"/"직접 지정")를 담아
  감사로그에서 어느 경로로 실행됐는지 구분 가능하게 함.
- 안전장치는 두 경로가 완전히 동일하다: 본인 권한 강등(su), 셸 미사용 argv, 파괴적 기본 명령
  거부(`catalog_exec_deny_commands`), /etc/hosts 등록 서버만, 타임아웃·출력 상한, 전건 감사로그.
- 지시문의 실행 섹션을 "확인해 달라는 요청 처리(가장 중요)"로 재작성 —
  1단계에서 **command.search_commands + manual.search_manual + System MCP 도구 목록** 세 곳을
  보고, 2단계에서 전용 도구가 있으면 그것을, 없으면 찾은 커맨드를 그대로 run_command에 넘기도록
  명시. "등록 여부와 무관하게 실행된다", "세 곳에서 못 찾으면 지어내지 말 것"도 함께 박아둠.
- 결과적으로 `myquota`를 카탈로그에 미리 등록할 필요가 없어졌다(NEXT-STEPS에서 등록 단계 삭제).
  매뉴얼에 적혀 있으면 그걸 찾아 실행한다.

## 53. 답변 중복 출력(스트리밍 버그) + 장황한 중계 + RAG 오검색 (완료)

리포트 3종: (a) 같은 문단이 두 번씩 출력됨, (b) "검색해 보겠습니다" 같은 중간 메시지와 출처가
장황하고 오류 시 매뉴얼의 일반 대처법(ssh 키 재생성)을 원인인 양 안내함, (c) "GPU 노드 접근"을
물었는데 CPU 내용을 답함.

**(a) 스트리밍 중복 — 실제 코드 버그.** `agent_server/main.py`의 SSE 핸들러가 `sent`(턴 전체
누적)를 기준으로 증가분을 계산했다. ADK는 한 메시지에 대해 '누적 텍스트' partial 이벤트들을
보내고 마지막에 같은 내용의 최종 이벤트를 한 번 더 보내는데, **툴 호출로 메시지가 여러 개 생기면
누적 기준이 메시지마다 새로 시작**한다. 그래서 두 번째 메시지부터는 `text.startswith(sent)`가
거짓이 되어 else 분기로 빠지고, partial과 최종 이벤트가 각각 전문을 통째로 재전송했다(= 모든
문단이 정확히 두 번). 비교 기준을 '턴 전체'에서 **'현재 메시지'**로 바꿔 해결
(`cur = text`로 교체, 전송 누계는 `full`로 분리). 스트리밍 3개 엔드포인트 모두 동일하게 수정.

**(b) 지시문 전면 재작성.** 기존 지시문이 "출처를 항상 표시"하라고 요구하고 있었고, 진행 상황
중계를 막는 규칙이 없었다. 답변 방식을 최상단으로 올려 (1) 진행 상황 중계 금지 (2) 최종 결과만
(3) 출처 표기 금지 (4) 실행 결과는 결과 자체만 (5) **실패 시 오류 메시지만 전하고 원인 추측/
해결책 창작 금지 — 특히 매뉴얼의 일반 대처법을 이 상황의 원인처럼 안내하지 말 것**을 명시.
지식 검색 항목에는 "검색 결과가 질문과 어긋나면 그대로 옮기지 말고 재검색"을 추가.

**(c) RAG 오검색 — 임베딩이 오염됐을 가능성이 높음.** 설정이 mock으로 초기화돼 있던 기간(#48)에
올린 문서는 임베딩이 NULL이거나 mock 벡터로 저장된다. NULL이면 벡터 검색 대상에서 빠져 키워드
검색만 남고, mock 벡터면 아예 엉뚱한 이웃을 물어온다 — "GPU 사용법"에 CPU 문서가 나오는 증상과
정확히 일치. 진단·복구 수단이 없어서 매번 추측하게 되므로 기능을 추가함:
- `GET /api/manuals/embedding-status` — 전체/불일치 청크 수 + 모델별 분포(현재 설정과 비교).
- `POST /api/manuals/reembed` — 임베딩이 없거나 현재 모델/차원과 다른 청크만 다시 임베딩
  (한 번에 300개, 남으면 이어서 호출). 임베딩 서버가 죽어 있으면 몇 개까지 했는지 알려주고 중단.
- `POST /api/commands/reembed` — 커맨드 카탈로그도 동일.
- 콘솔: 매뉴얼 탭 상단에 불일치가 있을 때만 경고 배너 + "현재 설정으로 다시 임베딩" 버튼
  (모델별 분포도 함께 표시해 원인 파악 가능). 커맨드 탭 기존 배너에도 버튼 추가.

**myquota 실행 실패는 아직 원인 미확정.** 에이전트가 받은 오류는 "인증 실패"였는데, 우리 실행
경로(ssh root → su - yr9.choi → myquota) 문제인지 myquota 자체가 사용자 ssh 키를 요구하는
것인지 구분이 안 된다. `docs/NEXT-STEPS.md` 8번에 서버에서 직접
`ssh root@202.20.185.100 "su - yr9.choi -c myquota"`를 실행해 비교하도록 요청함 — 직접 실행도
같은 오류면 우리 코드가 아니라 계정/환경 문제로 확정된다.

## 54. "이전엔 잘 됐는데 지금 CPU 답이 나온다" — 검색 진단 도구 추가 (조치)

재등록 후에도 GPU 질문에 CPU 문서가 나온다는 리포트. **"이전엔 됐다"가 핵심 단서라 코드 이력을
먼저 확인함**: `mcp_servers/manual_mcp/server.py`와 `shared/db.py`(임베딩·RRF·리랭크)는 이 저장소
히스토리 내내 **한 줄도 바뀌지 않았다**. 즉 검색 로직은 그때와 동일하고, 달라진 건 둘 중 하나다.

1. **설정값** — #48에서 전부 초기화된 뒤 내가 준 값으로 복구했는데, 그중 `rerank_provider`를
   RUN-LOG #22의 `curl .../8020/v1/models` 응답만 보고 `vllm`으로 찍어줬다. 리랭커가 TEI면
   payload 형식이 달라(`documents` vs `texts`) 400이 나고, `rerank()`는 **설계상 실패를 삼키고
   RRF 순위로 조용히 fallback**한다 — 리랭킹이 꺼진 채로 도는 것. 이전과 다른 가장 유력한 지점.
2. **임베딩 모델 id** — `vllm_embed_model`이 실제 서빙 id와 다르면 `embed_text`가 400을 내고,
   `search_manual`은 역시 **조용히 키워드 전용 검색으로 fallback**한다. 키워드만으로는
   "GPU 노드 접근"이 CPU 문서를 물어오기 쉽다.

두 fallback 모두 "조용히" 일어나서 밖에서는 구분이 안 되는 게 이 문제의 본질이었다. 그래서
추측을 멈추고 **보이게 만드는 도구**를 추가함:

- `POST /api/manuals/search-test` — Manual MCP의 `search_manual`과 **완전히 같은 쿼리**를 돌리되,
  숨겨지는 정보를 전부 노출한다: 검색 방식(하이브리드/키워드 전용), 임베딩 성공 여부와 차원 또는
  오류, 리랭커 실제 호출 결과(HTTP 상태·응답 앞부분·"provider가 서버 종류와 다를 수 있다" 힌트),
  발행 청크 수와 그중 임베딩이 있는 수, 상위 10건의 문서/섹션/본문과 RRF 점수 + 벡터순위(v)·
  키워드순위(k).
- 콘솔 매뉴얼 탭에 **검색 테스트** 입력창 추가 — 질문을 넣으면 에이전트가 실제로 받는 결과를
  그대로 보여준다. 벡터 순위(v)는 있는데 엉뚱한 문서가 위에 있으면 임베딩 문제, 키워드 순위(k)만
  붙어 있으면 임베딩 fallback, 리랭커 줄이 빨간색이면 provider 설정 문제로 바로 판별된다.
- `docs/NEXT-STEPS.md` 3번에 리랭커 종류를 확정하는 curl 두 줄(`/v1/rerank` vs `/rerank`)을 넣어
  `rerank_provider`를 추측이 아니라 응답으로 정하도록 함.

## 55. 답변 2회 출력 — #53 수정이 절반만 맞았음, 이벤트 플래그로 재수정 (완료)

#53에서 "비교 기준을 현재 메시지로" 바꿔 고쳤다고 했는데 현장에서 여전히 두 번씩 나온다는 리포트.
다시 보니 **문자열 비교만으로는 원리적으로 못 고치는 문제**였다.

ADK partial 이벤트의 text는 구현/경로에 따라 **'델타'일 수도 '누적'일 수도** 있다.
- #53 이전 코드(턴 전체 누적 기준): 델타형은 맞게 처리, **메시지가 여러 개면 깨짐**(툴 호출 시).
- #53 수정(현재 메시지 기준, `cur = text`): 누적형은 맞게 처리, **델타형이면 최종 이벤트가
  통째로 재전송됨**. 즉 한쪽을 고치면 다른 쪽이 깨지는 맞바꾸기였다.

근본 해결: 문자열 추측을 버리고 **`event.partial` 플래그로 메시지 경계를 잡는다**(ADK Event의
정식 필드임을 실제 모델 정의로 확인). `_StreamDedup` 클래스로 분리하고 규칙을 명시했다.
- partial 이벤트: 누적형이면 증가분만, 델타형이면 그대로 보낸다(`startswith`로 자동 판별).
- 최종(비-partial) 이벤트: partial을 이미 봤으면 **그 내용은 버린다**(이미 보냈으므로).
  partial이 하나도 없었으면(툴 응답 등) 그때만 전문을 보낸다.
- 최종 이벤트를 **메시지 경계로 보고 누적을 리셋** → 다음 메시지가 처음부터 다시 시작해도 안전.

델타형/누적형/최종만/툴 호출로 메시지 2개/최종 이벤트 2개/최종이 더 긴 경우 등 6가지 패턴을
실제로 실행해 전부 검증함(이전 두 구현은 각각 일부 패턴에서 중복 발생). 스트리밍 3개 엔드포인트
(`/v1/chat/completions`, `/v1/agent/query`, `/v1/voc/query`)가 모두 이 클래스를 쓴다.

주의: 이 수정은 agent-server 재시작이 있어야 반영된다. 반영 전에는 증상이 그대로 보인다.

## 56. 도구 호출 과정 웹에 표시 + 리랭커 형식 확정 (완료)

**(1) 리랭커 확정 — 사용자 curl 결과로 판명.** `/v1/rerank`에 `documents` 형식으로 보내면 정상
응답(gpu 0.699 vs cpu 0.217로 잘 구분함), `/rerank`에 `texts` 형식은 400(`documents` 필드 요구).
즉 **vLLM 형식이 맞다** — 설정이 `rerank_provider=tei`면 payload가 `texts`라 400이 나고
`rerank()`가 조용히 RRF로 fallback한다. 리랭커 자체는 GPU/CPU를 확실히 구분하므로, 리랭킹만
제대로 붙으면 "GPU 질문에 CPU 답" 증상은 사라질 가능성이 높다. 임베딩도 `bge-m3`로 정상 서빙 확인.

**(2) "중간에 생각하는 과정은 웹 상에서 보이게 해줘".** 앞서 요청("중간 메시지 내뱉지 마")과
상충하는 게 아니라, **답변 본문에 섞이는 건 싫고 과정은 보고 싶다**는 뜻으로 이해했다.
LLM에게 말로 설명시키지 않고(지시문은 여전히 중계 금지), **시스템이 실제 이벤트를 접히는
블록으로 내보낸다**:
- `_tool_activity_md()` — ADK 이벤트의 `get_function_calls()`/`get_function_responses()`를
  `<details><summary>도구 호출 · 툴이름</summary>` + JSON 본문으로 감싼다. Open WebUI에서
  평소엔 접혀 있고 클릭하면 펼쳐진다. 답변 본문·장기 메모리에는 들어가지 않는다.
- 설정 `show_tool_activity`(기본 true, hot_reload)로 끌 수 있다.
- 부수 효과가 크다: "command 실행이 전혀 안 된다"는 리포트를 **툴을 호출했는지 / 어떤 인자로
  호출했는지 / 어떤 에러가 돌아왔는지**를 사용자가 직접 보고 알려줄 수 있게 된다.

**(3) 문서 구조 변경(사용자 요청).** "RUN-LOG.md에는 작업 내용 전체가 아니라 agent를 서버에
띄우고 실행·셋업하는 과정만 담아라" → 기존 RUN-LOG.md를 `docs/HISTORY.md`로 옮기고(내용 보존),
`docs/RUN-LOG.md`는 배포·기동·재시작·점검 절차만 담은 문서로 새로 씀. CLAUDE.md의 필독 순서도
HISTORY 기준으로 갱신.

## 57. 커맨드가 에이전트로만 실패 — 컨테이너 ssh 키가 '빈 디렉토리'였을 가능성 (원인 확정적)

결정적 단서: **`ssh root@202.20.185.100 "su - yr9.choi -c myquota"`를 서버에서 직접 실행하면
정상 동작**한다. 우리 코드가 만드는 명령과 완전히 같은데 에이전트로만 실패한다는 뜻이므로,
차이는 '누가 실행하느냐'뿐 — 호스트 root가 아니라 **command-mcp 컨테이너**다.

컨테이너의 ssh 인증을 보니 원인이 나온다. `docker-compose.dev.yml`은
`${SSH_KEY_PATH:-./secrets/id_ed25519}:/root/.ssh/id_ed25519:ro`로 키를 마운트하는데,
저장소에 `secrets/` 디렉토리가 **아예 없다**. `.env`에 SSH_KEY_PATH를 지정하지 않으면 기본값인
`./secrets/id_ed25519`가 쓰이고, **docker는 없는 경로를 bind mount할 때 그 자리에 빈 디렉토리를
만든다.** 결과적으로 컨테이너 안 `/root/.ssh/id_ed25519`는 파일이 아니라 빈 디렉토리가 되고,
`ssh -i <디렉토리>`는 항상 인증 실패한다(exit 255, Permission denied (publickey)).
에이전트가 앞서 "인증에 실패" → "ssh 키를 재생성하라"고 잘못 안내한 것도 이 stderr 때문이었다.

조치:
- `shared/ssh_exec.py`: `SSH_KEY`가 **정규 파일일 때만** `-i`로 넘긴다(디렉토리면 무시하고 경고
  로그). ssh가 exit 255 + publickey/permission denied로 죽으면 `error` 필드에
  "ssh 인증 실패라 커맨드는 실행되지 않았음(사용자 권한 문제 아님) + SSH_KEY 경로가 파일이 아님/
  키가 대상 서버에 등록 안 됨" 중 해당하는 원인을 명시한다.
- `.env.example`: `SSH_KEY_PATH`를 `./secrets/id_ed25519` → `/root/.ssh/id_rsa`로 바꾸고,
  없는 경로를 적으면 빈 디렉토리가 생겨 인증이 깨진다는 경고를 주석으로 달았다.
- `docs/NEXT-STEPS.md` 1·3번에 컨테이너 안에서 키가 파일인지 확인하고, 컨테이너에서 직접
  ssh를 실행해보는 커맨드를 넣었다(성공해야 에이전트에서도 된다).

## 58. 지시문에서 정확한 함수명 제거 (완료)

요청: "왜 instruction에 직접적인 커맨드를 넣나. 관리자 콘솔에서 지정한 command mcp를 쓰게 해야지.
정확한 함수명을 넣지 말라."

`command.get_scheduler_job_info`, `command.search_commands`, `manual.search_manual` 같은 실제
함수명을 지시문에서 전부 제거하고 **역할로만** 기술했다("커맨드 카탈로그를 검색하는 도구",
"커맨드 실행 도구", "매뉴얼 검색 도구"). 맨 앞에 "도구는 관리자가 콘솔에서 수시로 추가/변경하니
이름을 외우지 말고 그때그때 도구 목록과 설명을 보고 고른다"는 원칙을 명시. 스케줄러 job 전용
섹션은 삭제했다 — 일반 "확인해 달라" 절차(검색 → 실행)로 자연히 처리된다.
이제 콘솔에서 도구를 추가/삭제해도 지시문을 고칠 필요가 없다.

## 59. "GPU 물었는데 CPU 답" — 검색이 아니라 LLM이 지어낸 것 (원인 정정)

임베딩 설정은 제대로 돼 있다는 사용자 확인. 그래서 임베딩 가설을 접고 **답변 내용 자체를**
다시 봤더니 결정적 증거가 있었다.

문제의 답변은 `#BSUB -q gpu`, `bsub < gpu_job.lsf`, `module load cuda/12.1` 같은 **LSF 문법**으로
가득했다. 그런데 이 시스템의 스케줄러 커맨드는 `phd`다(`command_mcp/server.py`의
`phd info -u <user>`). 즉 **매뉴얼에 없는 내용을 모델이 학습 지식으로 지어낸 것**이고,
사용자가 "CPU 사용법"이라고 표현한 것도 "우리 시스템 절차가 아닌 엉뚱한 일반론"이라는 뜻이었다.
검색 품질 문제로 오해하고 임베딩만 붙잡은 것은 내 오진이었다.

검색 파이프라인 코드도 다시 정독했으나 버그는 없었다: CSV 행 단위 청킹(`{열이름}: {값}`),
RRF 융합, `rerank()`의 정렬·상한 처리 모두 정상. 리랭커도 curl로 GPU/CPU를 0.699 vs 0.217로
확실히 구분함을 확인했다.

조치(지시문 재작성 — 근거 규칙을 최상단으로):
- "**일반적으로 알려진 방법을 우리 시스템 방법인 것처럼 답하지 않는다**"를 명시하고, 흔한 배치
  스크립트 문법·모듈 로드·접속 절차를 기억으로 만들어 쓰면 틀린 안내가 된다고 구체적으로 경고.
- "**도구를 호출하지 않은 채로 사내 절차를 설명하지 않는다. 먼저 조회하고 그다음에 답한다.**"
- "예시 코드·스크립트를 창작하지 않는다. 문서에 있는 명령만 그대로 옮긴다."
- 검색 결과가 어긋나면 재검색(최대 2회), 그래도 없으면 **모른다고 답한다**(기억으로 채우지 말 것).
- 앞서 요청된 것들(진행 상황 중계 금지, 출처 꼬리말 금지, 실패 시 원인 창작 금지, 도구 이름
  하드코딩 금지)은 그대로 유지하고 구조만 정리했다.

추가 조치: `llm_temperature` 설정(기본 0.2)을 만들어 `agent.py`가
`GenerateContentConfig(temperature=...)`로 적용하게 했다. 온도가 높으면 조회 결과 대신 그럴듯한
절차를 만들어내기 쉬워서, 근거 충실도를 위해 기본을 낮췄다(콘솔에서 조정 가능).

확정 방법: 콘솔 매뉴얼 탭 **검색 테스트**에서 `gpu 노드 접근`을 넣어 상위 결과가 실제 GPU 문서면
검색은 정상이고 위 지시문 문제로 확정된다(`docs/NEXT-STEPS.md` 4번).

## 60. 진행 상황 표시를 '답변 전에 갱신되는 상태 줄'로 정정 (완료)

#56에서 도구 활동을 `<details>` 접힘 블록으로 답변에 붙였는데, 요청은 그게 아니었다 —
**"진짜 답변 전에 한 줄이나 몇 줄로 계속 바뀌면서 보이는 그것"**, 즉 답변이 나오기 전까지
갱신되며 표시되고 답변이 시작되면 사라지는(접히는) 진행 표시였다.

Open WebUI는 스트림 본문의 `<think>...</think>` 영역을 그렇게 렌더링한다(스트리밍 중에는
내용이 갱신되며 보이고, 답변이 시작되면 접힌 상태로 정리됨). 그래서 표현을 바꿨다.
- `_tool_activity_md`(JSON을 통째로 details에 담던 것) → `_tool_status_lines`: 도구당 **한 줄**로
  줄인다. 호출은 `· 도구이름 — 첫 문자열 인자(60자)`, 결과는 `· 도구이름 → 3건 / 완료 /
  실패(exit 255) / 실패 — <error 첫 70자>`. `_short_result()`가 MCP 응답의 `result` 래핑,
  리스트 길이, `exit_code`, `error`를 각각 알맞게 줄인다.
- 스트리밍 루프: 첫 도구 활동에서 `<think>`를 열고 상태 줄을 흘리다가, **답변 텍스트가 시작되는
  순간 `</think>`로 닫는다.** 스트림이 도구 활동만 하고 끝나도 finally 전에 닫는다.
- 상태 줄은 `_StreamDedup`을 거치지 않으므로 **답변 본문·장기 메모리에 섞이지 않는다.**
- 실패 시 상태 줄에 원인이 그대로 보인다(예: `· run_command → 실패 — ssh 인증 실패로 실행되지
  않음`) — 커맨드가 왜 안 되는지 사용자가 바로 확인할 수 있다.
- 도구 호출 2회 + 응답 2회 + partial 답변 시나리오로 출력 형태와 메모리 저장 내용을 검증했다.

## 61. 커맨드 실행 실패의 진짜 원인은 Host key verification (정정)

#57에서 "compose가 없는 경로를 마운트해 빈 디렉토리가 됐을 것"이라고 추정했으나 **틀렸다.**
사용자가 확인한 컨테이너 안 키는 `-rw------- 1 root root 399 /root/.ssh/id_ed25519` — 정상
개인키 파일이었다(즉 `.env`에 SSH_KEY_PATH가 이미 제대로 잡혀 있었다). 실제 에러는
**`Host key verification failed.`** 였다.

다만 사용자가 실행한 테스트 명령에는 우리 코드가 쓰는 옵션이 빠져 있었다
(`StrictHostKeyChecking=accept-new` 없음) — BatchMode에서는 호스트 키를 물어볼 수 없어 기본값
`ask`가 곧바로 실패한다. 그래서 그 테스트만으로는 우리 실행 경로도 같은 이유로 실패하는지
확정되지 않는다. `docs/NEXT-STEPS.md` 2번에 **우리 코드와 동일한 옵션**으로 실행하는 명령을 넣어
비교하도록 했다.

그와 별개로 호스트 키 문제가 실제로 우리 경로를 막을 수 있는 구멍이 있어 코드를 손봤다.
컨테이너에는 known_hosts가 없고 재생성될 때마다 초기화되는데, 이미지/홈에 남은 항목과 충돌하면
`accept-new`로도 거부된다(accept-new는 '처음 보는 호스트'만 허용하고 '키가 바뀐 경우'는 막는다).
- `SSH_KNOWN_HOSTS`(기본 `/root/.ssh/known_hosts_agent`)를 명시해 호스트 쪽 known_hosts와 섞이지
  않는 컨테이너 전용 파일을 쓰게 했다.
- `SSH_STRICT_HOST_KEY`(기본 `accept-new`)를 환경변수로 뺐다. 게이트 서버 키가 바뀌어 계속 막히면
  `.env`에 `SSH_STRICT_HOST_KEY=no`만 넣고 컨테이너를 재생성하면 된다(compose의 command-mcp·
  system-mcp에 배선).
- stderr에 `host key verification`이 있으면 `error` 필드에 "호스트 키 확인 실패라 커맨드가 실행되지
  않았음(인증/계정 문제 아님) + 어떻게 푸는지"를 명시하도록 분기를 분리했다. 기존 publickey
  분기와 섞여 있어 원인이 뭉뚱그려졌던 것을 나눴다.

또한 사용자가 `.env`에 `SSH_KEY_PATH=/root/.ssh/id_rsa`를 추가했는데, 게이트 서버에 등록된 키는
컨테이너에 이미 마운트돼 있던 ed25519다. 이 줄은 오히려 동작하던 키를 바꿔 버리므로 제거하도록
NEXT-STEPS 1번에 넣었다.

## 62. 커맨드 실행 실패의 원인은 TTY 부재로 추정 확정 + 진행 표시/환각 보강 (조치)

**결정적 데이터**: 사용자가 컨테이너 안에서 우리 코드와 동일한 ssh 옵션으로 실행하니
`myquota`가 **정상 동작**했다(gpfs.gpu1 사용량 출력). 즉 ssh 키·호스트 키·계정·원격 커맨드는
전부 정상이고, #57(빈 디렉토리)·#61(호스트 키)은 둘 다 원인이 아니었다.

그런데 에이전트로 물으면 여전히 실패한다. 손으로 돌린 것과 우리 코드의 차이는 하나뿐 —
**TTY**. `docker compose exec`는 기본으로 pty를 붙이지만, `asyncio.create_subprocess_exec`로
띄우는 ssh에는 pty가 없다. `su - <user> -c ...`는 PAM 설정(pam_securetty 등)에 따라 TTY가 없으면
인증 단계에서 거부될 수 있고, 그 경우 stderr가 "authentication failure"류로 나온다 — 에이전트가
"인증 오류"라고 답한 것과 일치한다.

조치:
- `ssh -tt`로 TTY를 강제(`SSH_FORCE_TTY`, 기본 true, `.env`에서 false로 끌 수 있음).
  손으로 돌려 성공한 조건과 동일해진다.
- pty를 붙이면 입력 대기에 걸릴 수 있어 `stdin=DEVNULL`을 명시.
- pty 출력의 CRLF를 LF로 정규화하고 `Connection to ... closed.` 안내 줄을 제거해 결과를 깨끗하게.
- 확정용 A/B 테스트를 `docs/NEXT-STEPS.md` 1번에 넣었다: 성공했던 명령에 `-T`(TTY 끄기)만 붙여
  실패하면 원인 확정.

**진행 상황 표시(#60 후속)**: `<think>`로 감싸니 Open WebUI가 "생각 중"으로 접어버려 내용이 안
보인다는 리포트. 펼쳐진 `<details open><summary>진행 상황</summary>` 블록으로 바꿔 한 줄씩
그대로 보이게 했다. 답변이 시작되면 블록을 닫는다(답변 본문·장기 메모리에는 여전히 안 들어간다).

**환각 보강**: GPU 답변이 매뉴얼 기반으로 개선됐으나 "H100은 -arch=compute_90..." 같은 우리
인프라에 없는 일반 지식을 덧붙이는 문제. 지시문 근거 규칙에 "**조회한 내용에 없는 것을 덧붙이지
않는다** — 하드웨어별 컴파일 옵션·성능 팁·주의사항·추가 예시를 스스로 붙이지 말고, 조회된
범위에서 끝내고 더 필요하면 '문서에는 여기까지'라고 밝힌다"를 추가했다.

## 63. 근거 규칙을 질문 유형별로 분리 (완료)

#62에서 "조회 결과에 없는 것을 덧붙이지 말라"와 "예시 코드·스크립트를 창작하지 말라"를 넣었더니
범위가 과했다. 사용자 정정: **"아주 일반적인 linux 기본 command나 python 등의 코드 문법은
사용해도 된다. 대신 우리 인프라 활용법 가이드는 무조건 MCP를 통해서 답변해야 하고, 일반 지식은
붙이지 마라."**

지시문의 근거 규칙을 두 갈래로 나눴다.
- **(A) 우리 인프라 활용법** — 접속·계정·스토리지·할당량·스케줄러·큐·정책, 사내 전용 커맨드,
  서버 이름/경로 등. 도구로 조회한 내용만 답하고, 조회 범위 밖의 일반 지식(하드웨어별 컴파일
  옵션, 성능 팁, 주의사항, 추가 예시)을 덧붙이지 않는다. 없으면 없다고 답한다.
- **(B) 일반 지식** — 표준 리눅스 명령어 사용법, 셸/Python 문법, 에러 메시지 해석, 개념 설명.
  도구 조회 없이 답해도 되고 예시 코드도 써도 된다. 단 **여기에 우리 인프라 이야기(서버 이름·
  경로·큐 이름·사내 커맨드)를 추측해 섞지 않는다** — 섞이는 순간 (A)이므로 조회부터 한다.
- 경계가 애매하면 (A)로 간주.

이전 문구("예시 코드·스크립트를 창작하지 않습니다")는 (A)에만 해당하도록 흡수해 삭제했다.

## 64. 섞인 질문 처리 규칙 추가 (완료)

#63의 (A)/(B) 분리에 이어, **한 질문에 두 유형이 섞이는 경우**의 규칙을 요청받음.
예시: "GPU 노드 접근해서 내 파일 리스트 보는 방법 알려줘" — 앞부분은 (A) 인프라 활용법,
뒷부분은 (B) 일반 리눅스 명령.

지시문에 (C)절을 추가했다.
- 부분별로 나눠 답하고 하나의 답으로 이어 붙인다((A)는 조회 결과대로, (B)는 일반 지식으로).
- (A) 부분을 **먼저 조회**한다. 조회 결과에 없으면 **그 부분만** "확인되지 않습니다"라고 하고
  (B) 부분은 정상적으로 답한다 — 전체를 못 답한다고 하지 않는다(과잉 거절 방지).
- (B) 부분에서도 사내 고유값(홈 경로, 서버 이름, 큐 이름, 사내 커맨드)을 지어내지 않는다.
  그게 필요해지면 그건 (A)이므로 조회하거나 확인되지 않았다고 밝힌다.

## 65. VOC 업로드를 '형식 고정'에서 '헤더 자동 인식 + 열 매핑'으로 (완료)

기존 VOC 임포트(`POST /api/voc/import`)는 **두 형식만** 받았다 — (1) 1행 헤더 Question/Answer,
(2) 사내 표준 4행 헤더(의뢰내용/조치일/처리내용/만족도). 그 외 엑셀은 422로 거부됐고, 제외 규칙
(만족도 불만족류, 조치일 없는 행)도 코드에 하드코딩돼 있었다.

**헤더 행 자동 인식**(`spreadsheet.detect_header_row`): 위쪽 15행을 훑어 각 행을 헤더 후보로
점수화한다 — 채워진 칸 수, 값이 서로 다른 정도, 아래로 3행이 비슷한 폭으로 이어지는지에 가점,
한 칸에 40자 넘는 긴 문장이 있으면(제목 줄) 감점. 1행부터 표가 시작하는 파일, 2행에 제목이 있고
4행부터 표가 나오는 파일, 제목이 3줄인 파일을 실제 엑셀로 만들어 전부 올바른 행을 찾는 것을 확인.
빈 행을 제거하지 않고 세기 때문에 **표시되는 헤더 행 번호가 엑셀에서 보는 실제 행 번호와 일치**한다.
자동 판별이 틀리면 UI에서 실제 행 번호를 입력해 다시 읽을 수 있다.

**열 매핑 업로드**(`/api/voc/excel/preview` → `/excel/commit`): 매뉴얼·커맨드 탭과 같은 흐름.
- 질문/답변 열은 필수, 부서 열은 선택.
- 하드코딩돼 있던 제외 규칙을 일반화했다: `exclude_column` + `exclude_values`(예: 만족도가
  불만족/매우불만족이면 제외), `require_columns`(예: 조치일이 비면 제외).
- 사내 표준 포맷이면 열 이름으로 매핑을 미리 채워준다(의뢰내용→질문, 처리내용→답변,
  만족도→제외 기준 + "불만족, 매우불만족" 기본값) — 기존 파일은 클릭 두 번이면 끝난다.
- `.csv`도 받는다(voc_db에 upload_sessions 테이블 추가, 마이그레이션 voc_db v5).

부수 효과: 매뉴얼·커맨드 탭도 같은 함수를 쓰므로 **제목 줄이 위에 있는 엑셀을 이제 그대로 받는다**
(이전에는 무조건 1행을 헤더로 봤다). 두 탭의 열 선택 화면에도 인식된 헤더 행을 표시한다.

기존 `/api/voc/import`(자동 인식 2형식)는 그대로 두었다 — 이미 쓰던 스크립트가 있을 수 있어서.

## 66. VOC 활용 규칙 + 개인정보 마스킹 + 답변 유형 분기 (완료)

**(1) 개인·조직 정보 마스킹 — 코드 1차, 지시문 2차.**
VOC는 실제 문의 원문이라 계정·이메일·이름·부서가 그대로 들어 있다. 지시문으로만 막으면 원문이
이미 프롬프트에 들어간 뒤라 유출 경로가 남으므로, **MCP가 결과를 돌려주기 전에** 지운다
(`shared/pii.py`, VOC MCP가 question/answer/department에 적용).
- 이메일 → `{사용자 id}` / 사내 계정(`yr9.choi`처럼 점 앞에 숫자가 있는 형태) → `{사용자 id}`
- 조직 접미사(사업부·본부·부문·센터·그룹·파트·모듈·부서·팀) → `{사업부명}` `{팀명}` 등
- 이름: 성씨 + 호칭/직급(책임·선임·님 등)이 뒤따르는 경우, 그리고 `담당자:`/`요청자:` 라벨이
  붙은 경우만(영문 이름 포함). 단독 2~3글자를 이름으로 보면 오탐이 커서 잡지 않는다.
- 오탐 방지를 실제로 검증함: `server.log`, `app.conf`, `nvidia-smi`, `python3.11`은 그대로 유지.
- 정규식이 못 잡는 표기(외국 이름 등)를 위해 지시문에도 "식별 정보는 자리표시자로 바꿔 쓰고,
  자리표시자를 실제 값으로 추측해 채우지 않는다"를 넣었다. 단, **본인 계정으로 실행한 결과는
  그대로 보여준다**(본인 정보까지 가리면 쓸모가 없어짐).

**(2) VOC 답변 유형 분기.** 같은 VOC라도 사용자가 스스로 할 수 있는 건과 운영자가 시스템을
직접 확인해야 했던 건은 답이 달라야 한다. `voc_mcp.classify_handling()`이 답변 텍스트에서 사람이
개입한 흔적(확인 결과/점검해보니/재기동/권한 부여/조치 완료 등)을 찾아 `handled_by`를
`"user"|"operator"`로 붙여 준다(공백 무시 비교, 애매하면 operator 쪽으로).
지시문에는 판단 기준과 대응 원칙만 적고 **문구는 상황에 맞게 만들어 쓰도록** 했다 —
operator 건이면 방법을 안내하지 말고, 운영팀이 확인·조치한 사례가 있다는 점 + 접수 안내.

**(3) 접수 경로는 설정으로.** `voc_intake_guide` 설정 키를 추가하고 `agent.py`가 매 요청
지시문 끝에 붙인다(로그인 서버 이름과 같은 방식). 포탈 메뉴가 바뀌어도 지시문을 고칠 필요가 없다.
기본값은 임시 문구라 **콘솔에서 실제 경로로 바꿔야 한다**(NEXT-STEPS 7번).

**(4) VOC 검색 호출 조건 명확화.** 기존에는 지시문 맨 아래 한 줄이라 잘 호출되지 않을 구조였다.
"증상·오류·장애처럼 선례가 있을 법한 질문이거나 매뉴얼만으로 부족하면 VOC도 본다",
"매뉴얼에 공식 절차가 있으면 그것을 먼저 쓰고 과거 사례는 보조로 덧붙인다"로 정리했다.

---

## 다음 항목은 이어서 여기 아래에 추가
