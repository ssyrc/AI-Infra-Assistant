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

> **[정정 — #74 참조]** 여기서 예로 든 `-arch=compute_90` 재컴파일 안내는 **매뉴얼에 실제로
> 있는 내용**이었다. 이 건은 환각이 아니었다. 규칙 자체("조회 결과에 없는 것을 덧붙이지
> 않는다")는 유효하지만, **컴파일 옵션을 예시로 삼은 것은 잘못**이다.
> 앞으로 이 항목을 근거로 "컴파일 옵션이 나오면 환각"이라고 판단하지 말 것.

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

## 67. 검색 결과 관련성 검증 추가 (완료)

"RAG로 가져온 문서가 진짜 필요한 건지 검증하고, 필요 없으면 쓰지 말라"는 요청. VOC도 매뉴얼과
동일한 하이브리드 RAG를 타므로 두 곳 모두에 적용되게 **두 겹**으로 넣었다.

**(1) 코드 — 리랭커 점수 하한(`rerank_min_score`, 기본 0.05).**
기존에는 리랭킹 후 상위 top_k를 그대로 돌려줬다. RRF 상위라는 건 "후보 중 상대적으로 나은 것"일
뿐이라, 질문과 무관한 문서라도 후보가 적으면 그냥 1등이 되어 근거로 쓰였다(GPU를 물었는데 CPU
문서가 올라온 상황이 정확히 이것). `shared/db.rerank()`가 점수 하한 미만 항목을 버리고, 전부
미달이면 **빈 리스트**를 돌려준다 → 에이전트는 "검색 결과 없음"으로 보고 지시문에 따라
"확인되지 않습니다"로 답한다. 몇 건을 왜 버렸는지 로그로 남긴다.
- **리랭커가 실제로 점수를 매긴 경우에만** 적용한다. 리랭커 장애 시 fallback은 전 항목 0.0점이라
  하한을 적용하면 검색이 통째로 죽어버리므로 그 경로는 건드리지 않는다.
- 사용자 curl 기준값 참고: 같은 주제 0.699 / 다른 주제(gpu↔cpu) 0.217. 기본 0.05는 "명백히 무관"만
  거르는 보수적인 값이고, 더 엄격하게 하려면 0.1~0.2로 올린다(콘솔에서 조정, 재시작 불필요).

**(2) 지시문 — 내용 기반 검증 절차.** 점수는 주제 유사도일 뿐 "이 질문에 답이 되는가"와는 다르다.
그래서 도구 사용 절에 공통 절차를 넣었다: 받은 항목을 하나씩 보고 (1) 지금 질문에 실제로 답하는지
확인 — 주제가 같아 보여도 대상(GPU/CPU, 서버/개인 계정, 신청/조회)이 다르면 버린다,
(2) 관련 없는 항목은 **답변에 언급조차 하지 않는다**(참고로도 붙이지 않음), (3) 남는 게 없으면
재검색 후 "확인되지 않습니다", (4) 일부만 답하면 그 부분만 쓰고 나머지는 확인 안 됐다고 밝힌다.
VOC 절에도 "증상은 비슷한데 원인·대상이 다른 사례는 쓰지 않는다 — 억지로 끼워 맞춘 사례는
없느니만 못하다"를 추가했다.

**(3) 진단 화면에 하한 표시.** 매뉴얼 탭 검색 테스트에 현재 `rerank_min_score`를 함께 보여준다 —
결과가 비었을 때 "임베딩이 죽은 건지, 하한에 걸린 건지"를 구분할 수 있다.

## 68. RAG 개선 5종 적용 — 한국어 검색·쿼리확장·문맥주입·오버랩·중복제거 (완료)

정리 답변에서 지적한 격차 5개를 전부 구현했다. **폐쇄망 제약 때문에 새 pip 패키지·형태소 사전
설치 없이** 되는 방법을 골랐다.

**(1) 한국어 검색 — 가장 큰 구멍이었다.**
`to_tsvector('simple')`은 공백 토큰화만 해서 "접근하려면"이 "접근"과 매칭되지 않고,
`plainto_tsquery`는 토큰을 **AND**로 묶어 하나라도 없으면 0건이었다. 사실상 키워드 축이 죽어
있었고 벡터 축만 돌고 있었다. 두 가지로 고쳤다.
- `plainto_tsquery` → **OR 질의**(`to_tsquery('simple', 'gpu | 노드 | 접근')`).
  `shared/retrieval.ts_or_query()`가 토큰화 + 조사/어미 제거본까지 함께 넣는다.
- **pg_trgm 문자 3-gram을 세 번째 검색 축으로 추가**(Postgres 기본 contrib라 오프라인 설치 불필요).
  RRF가 2축 → **3축**(벡터 + 키워드 + 3-gram)이 됐다. 마이그레이션이 안 돌았거나 확장이 없는
  환경에서도 죽지 않도록 `has_trgm()`이 `pg_extension`을 1회 확인하고 없으면 기존 2축으로 동작한다.
- 주의: asyncpg는 `$1` 파라미터를 쓰므로 `%`를 이스케이프하면 안 된다. 처음에 `%%`로 썼다가
  SQL 문법 오류가 될 뻔한 것을 잡아 `%`로 고쳤다.

**(2) 쿼리 확장.** `expand_query()`가 원문 + 조사/어미 제거본 + 핵심어 버전을 만들어 키워드·3-gram
축에 함께 넣는다. LLM 호출이 없어 지연이 늘지 않고, **첫 변형이 항상 원문**이라 확장이 도움이 안
되는 경우에도 기존 품질을 해치지 않는다.

**(3) Contextual retrieval(문맥 주입).** 청크 본문만 임베딩하면 "무엇에 대한 문서인지"가 벡터에
안 들어간다. 특히 엑셀/CSV는 한 행이 한 청크라 본문이 짧아 문맥이 거의 없다.
- 임베딩 입력을 `문서 제목 > 섹션 제목\n본문`으로 바꿨다(본문 저장은 그대로).
- 리랭커 입력도 같은 형태로 바꿨다.
- tsvector에도 `section_title`을 포함하도록 재생성했다(manual_db v6).
- **기존 문서는 재임베딩해야 적용된다** — 매뉴얼 탭 "현재 설정으로 다시 임베딩" 버튼.

**(4) 청크 오버랩.** `_split_long_text()`가 앞 조각의 끝 200자를 다음 조각 앞에 겹쳐 준다
(공백 기준으로 보정해 단어 중간에서 시작하지 않게). 경계에 걸친 문장이 어느 조각에서도
온전하지 않던 문제를 없앤다. 새로 업로드하는 문서부터 적용된다.

**(5) MMR 중복 제거.** 리랭킹을 `top_k*2`로 넉넉히 받은 뒤, 3-gram 자카드 유사도가 기준
(`dedup_similarity`, 기본 0.85) 이상인 항목을 버리고 top_k를 채운다. VOC처럼 유사 문의가 반복
등록되는 데이터에서 상위가 같은 사례로 도배되는 걸 막는다.

적용 범위: Manual·VOC·Command MCP **셋 다**. 새 설정 키는 `dedup_similarity` 하나이고,
마이그레이션은 manual_db v6 / voc_db v6 / command_db v6(pg_trgm + 인덱스)이라
**db-init 재실행이 필요**하다.

## 69. `myquota` 실패 원인 확정 — TTY가 아니라 **25초 타임아웃**이었다 (완료)

**A/B 테스트 결과.** NEXT-STEPS 1번(우리 코드와 같은 조건 = `exec -T`로 TTY 없이 실행)을 돌려보니
**성공**했다.

```
docker compose -f docker-compose.dev.yml exec -T command-mcp \
  ssh -o BatchMode=yes ... root@202.20.185.100 "su - yr9.choi -c myquota" < /dev/null
Filesystem    Directory       Used(GB)   Limit(GB)       Files File limits
gpfs.gpu1     yr9.choi          10,555           0   4,202,829           0
```

→ **TTY 가설은 틀렸다.** `su - <user> -c ...`는 TTY 없이도 정상 동작한다. 손으로 돌린 것과
에이전트가 돌린 것 사이에 남은 차이는 하나뿐이었고, 사용자가 힌트를 줬다: **"시간이 좀 오래 걸린다".**

**원인.** `shared/ssh_exec.py`의 `DEFAULT_TIMEOUT = 25`. GPFS 쿼터 조회(`myquota`)는 스토리지
전체를 훑어서 25초를 넘긴다. 타임아웃이 걸리면 우리는 프로세스를 kill하고 `TimeoutError`를 던지는데,
에이전트 입장에서는 그냥 "실패"라 **권한/인증 문제인 것처럼 엉뚱하게 해석한 답변**을 만들었다.
정상 동작하는 커맨드를 우리가 중간에 끊고 있었던 것이다.

**조치.**
- `DEFAULT_TIMEOUT`을 환경변수 `SSH_COMMAND_TIMEOUT`으로 빼고 기본값 25 → **120초**.
  compose의 command-mcp/system-mcp 양쪽에 `SSH_COMMAND_TIMEOUT: ${SSH_COMMAND_TIMEOUT:-120}` 추가.
- 타임아웃 메시지에 "원래 오래 걸리는 커맨드라면 `SSH_COMMAND_TIMEOUT`을 늘리세요
  (**권한/인증 문제가 아닙니다**)"를 넣었다. 다음에 또 걸려도 원인을 오해하지 않게.
- `SSH_FORCE_TTY` 기본값을 `true` → **`false`** 로 되돌렸다. TTY가 원인이 아님이 확인됐고,
  `ssh -tt`는 출력에 CR/제어문자와 `Connection to ... closed.` 안내를 섞어 결과를 더럽힌다.
  TTY를 요구하는 환경이 나오면 `.env`에서 켠다.

**교훈.** "손으로 하면 되는데 에이전트로는 안 된다"에서 곧바로 권한/인증을 의심했지만(빈 키 파일,
호스트 키, TTY — 셋 다 틀렸다), 실제 차이는 **실행 시간**이었다. 조건을 하나씩 지우는 A/B를
사용자에게 돌려달라고 한 것이 결국 정답이었다.

## 70. 진행 상황 표시를 사람 말로 요약 — 도구 이름 비노출 (완료)

기존에는 `· run_command — myquota` / `· run_command → 완료`처럼 **도구 이름과 인자 원문**을
그대로 뿌리고 있었다. 사용자에게 의미가 없고 내부 구현이 드러난다. 요청대로 "무엇을 하는 중인지"만
한 줄 문장으로 요약한다.

`agent_server/main.py`에 `_action_phrase()` / `_result_phrase()`를 넣었다.
- 도구 이름을 **부분 문자열**로 매칭한다(`manual`/`voc`/`command`/`job`/`gpu`/`disk`/`file`/`dir`
  /`system`, 그리고 `run`·`exec`→실행, `search`·`find`→검색). 관리자가 콘솔에서 새 도구를 추가해도
  규칙이 그대로 먹고, 매칭이 안 되면 "확인하는 중"으로 떨어져 이름이 새지 않는다.
- 인자는 첫 문자열 값만 40자로 잘라 따옴표로 감싼다(질문 키워드가 보이는 게 유용하므로).
- 결과는 리스트면 "N건 찾음"/"찾은 내용 없음", `exit_code`면 "완료"/"실패(종료코드 N)",
  `error`면 "실패 — <사유 60자>"로 요약한다.

예시: `· 매뉴얼에서 'gpu 노드 접근' 찾는 중` → `· 3건 찾음` → `· 'myquota' 실행하는 중` → `· 완료`.

`<details open>` 블록은 그대로다(Open WebUI가 답변 전에 펼친 채로 보여준다). 실패 시 사유가
이 블록에 남으므로 디버깅용으로도 계속 쓸 수 있다.

## 71. `Context variable not found: 사업부명` — ADK가 지시문의 `{...}`를 상태 변수로 치환한다 (완료)

**증상.** "안녕" 한 마디에도 `[오류가 발생했습니다: 'Context variable not found: 사업부명.']`.
질문 내용과 무관하게 **모든 요청**이 실패했다.

**원인.** ADK는 `instruction`이 **문자열**이면 매 LLM 호출 전에
`inject_session_state()`로 `{...}`를 세션 상태 변수로 치환한다
(`google/adk/utils/instructions_utils.py`). 정규식 `r'{+[^{}]*}+'`로 잡은 뒤,
이름이 유효한 식별자인데 상태에 없으면 `KeyError`를 던진다.

우리가 #66에서 넣은 개인정보 자리표시자가 여기 걸렸다.
```python
'사업부명'.isidentifier()   # True  ← 한글도 파이썬 식별자다 → 상태 조회 → KeyError
'사용자 id'.isidentifier()  # False ← 공백이 있어 식별자가 아님 → 그대로 통과
```
그래서 `{사업부명}` `{팀명}` `{센터명}` `{그룹명}` `{파트명}` 처럼 **한 단어짜리 한글
자리표시자만** 터졌고, `{사용자 id}` `{사용자 이름}`은 멀쩡했다. 에러가 하필 `사업부명`을
가리킨 것도 이 때문이다(앞에서부터 처음 걸린 것).

**조치.** `instruction`을 문자열 대신 **콜러블(InstructionProvider)** 로 넘긴다.
`LlmAgent.canonical_instruction()`이 문자열이 아니면 `bypass_state_injection=True`를 반환해
치환 단계를 통째로 건너뛴다.

```python
def instruction_provider(_ctx=None, _text=instruction) -> str:
    return _text
agent = Agent(..., instruction=instruction_provider, ...)
```

설치된 ADK로 재현·검증했다 — 문자열이면 `KeyError: 'Context variable not found: \`사업부명\`.'`,
콜러블이면 `bypass_state_injection=True`에 자리표시자가 원문 그대로 유지된다.

**부수 효과(의도한 것).** 장기 메모리(`extra_instruction`)로 붙는 **사용자 대화 내용에
중괄호가 섞여도** 이제 안전하다. 예전 방식이었다면 사용자가 `{foo}`를 한 번 말하는 순간
그 대화는 영구히 죽었을 것이다. 자리표시자를 다른 기호로 바꾸는 대신 콜러블을 택한 이유가 이것이다.

**교훈.** 지시문은 우리가 쓰는 평범한 텍스트가 아니라 **ADK의 템플릿**이다.
지시문에 리터럴 중괄호를 넣어야 하면 반드시 콜러블로 넘긴다.

## 72. 3-gram 검색축이 **처음부터 한 건도 반환하지 않고 있었다** (완료)

#68에서 "3축 RRF(벡터+키워드+3gram)"를 넣었다고 기록했는데, 실제로는 **2축으로 돌고 있었다.**

**원인.** 세 MCP 모두 이렇게 썼다.
```sql
WHERE chunk_text % $query        -- pg_trgm.similarity_threshold(기본 0.3) 이상일 때 참
ORDER BY similarity(chunk_text, $query) DESC
```
`similarity(a,b)`는 **두 문자열 '전체'의 3-gram 자카드**다. 문서가 길면 분모(합집합)가 커져
값이 0으로 수렴한다. 실제 청크 길이로 계산해 보면:

| 문서 | similarity | word_similarity |
|---|---|---|
| 짧은 설명(37자) | 0.205 | 0.368 |
| 실제 청크(514자) | **0.039** | 0.368 |

즉 질의의 3-gram이 문서에 **전부** 들어 있어도 0.3을 못 넘는다. `%` 조건이 **항상 거짓**이라
`trgm_search` CTE는 늘 0건이었고, RRF는 벡터+키워드 2축으로만 돌았다. 한국어 키워드 축은
조사/어미 때문에 원래 약한데, 그걸 보완하라고 넣은 축이 죽어 있었으니 사실상 **벡터 검색
하나로 버티고 있던 셈**이다. "gpu 물었는데 cpu 답" 류의 증상이 계속된 배경이 이것이다.

**조치.** `similarity(문서, 질의)` → **`word_similarity(질의, 문서)`** 로 교체.
word_similarity는 "문서 안에서 질의와 가장 잘 맞는 **구간**"을 보므로 문서 길이에 휘둘리지
않는다. 임계값은 설정 키 `trgm_min_similarity`(기본 0.3)로 뺐다.
Manual·VOC·Command MCP 세 곳 모두 적용.

트레이드오프: `word_similarity(...) >= 임계값` 형태는 GIN 인덱스를 타지 않는다(인덱스는
연산자 `<%`만 지원). 하지만 인덱스를 타던 `%`는 위처럼 **절대 참이 되지 않아** 무의미했다.
청크 수천~수만 규모에서 순차 평가는 수십 ms라 정확도를 택했다.

## 73. 매뉴얼 검색 경로 일원화 + 이웃 청크 확장 (완료)

**(1) 콘솔 '검색 테스트'가 실제 검색과 다른 SQL을 쓰고 있었다.** 콘솔은 `plainto_tsquery`
2축, MCP는 `to_tsquery` OR 3축. 그래서 "콘솔에선 나오는데 챗봇은 못 찾는다"를 진단할 방법이
없었다 — 지금까지 디버깅이 유난히 오래 걸린 구조적 이유다.
→ `shared/manual_search.py`로 검색을 **한 곳에** 모으고, Manual MCP와 콘솔 검색 테스트가
**같은 함수**를 호출한다. 이제 콘솔 결과가 곧 챗봇 결과다(축별 RRF 기여도 `vrrf/krrf/trrf`와
`trgm_min_similarity`도 함께 보여준다).

**(2) 절차 문서에서 중간 단계가 사라지는 문제.** "슈퍼컴 계정 신청" 답변에서
`1. USER SUPPORT > 슈퍼컴 서비스 > 서비스 신청` 단계가 빠졌다. CSV 파싱을 실제 데이터로
재현해 봤더니 **파싱·정제는 멀쩡했다**(원문 91자 → 정제 91자, 첫 줄 보존). 진짜 원인은
**청크 단위**였다. 엑셀/CSV는 한 행(=한 페이지)이 한 청크라, 검색이 2단계와 4단계만 집어오면
3단계는 애초에 컨텍스트에 없다.
→ **이웃 청크 확장**: 검색된 청크의 앞뒤(`manual_neighbor_window`, 기본 1)를 같은 문서에서
가져와 본문에 이어 붙인다. 관련도 순서는 리랭킹 결과 그대로 두고 '읽을 범위'만 넓힌다.
seq 1·3만 검색돼도 1~5단계가 전부 들어온다(단위 테스트로 경계·누락 문서까지 확인).

## 74. 실행 요청인데 사용법만 답하던 문제 / 문서 위치 안내 (완료)

**(1) "내 홈 파일 리스트 보여줘" → `ls -l` 설명만 하고 실행을 안 했다.**
지시문 (C)의 예시가 하필 *"파일 리스트 보기는 (B) 일반 지식"* 이라고 못 박아 둔 탓이다.
내가 쓴 예시가 그대로 오답을 유도했다.
→ (B)에 갈래를 넣었다: **(B)는 "어떻게 하느냐"를 물었을 때만.** 사용자가 자기 자원의
상태·내용을 **보여 달라/확인해 달라**고 하면 표준 리눅스 명령으로 되는 일이라도 실행해서
결과로 답한다. 애매하면 실행(읽기 전용이라 안전하고, 원한 건 대개 결과다).

**(2) "OO 문서를 참고하세요"로는 문서를 찾을 수 없다.**
→ `manual_files.reference_path` 컬럼 추가(manual_db v7) + 콘솔 문서 상세에 입력란 +
검색 결과에 실려 나가고, 지시문에 "제목만 말하지 말고 이 경로를 그대로 적는다"를 넣었다.
경로가 비어 있으면 지어내지 않는다.

**(3) ~~문서에 없는 CUDA 아키텍처 플래그를 계속 붙이는 문제~~ — 오판이었다(철회).**
`-arch=compute_90` 류의 재컴파일 안내는 **매뉴얼에 실제로 있는 내용**이었다(사용자 확인).
환각이 아니라 정상 동작이었는데, 나는 #66부터 이걸 계속 환각으로 취급해 왔다.

처음에 지시문 (A)에 "아키텍처 플래그·모듈 로드 순서·배치 지시자·환경변수 예시·권장 파라미터는
절대 쓰지 않는다"는 **금지 목록**을 넣었는데, 이건 **문서에 있는 내용까지 억누르는 규칙**이라
그대로 뒀으면 답변 품질을 오히려 떨어뜨렸을 것이다. 즉시 걷어냈다.

대신 남긴 것은 방향이 반대인 두 줄이다:
- "답을 다 쓴 뒤 문장 하나하나가 조회 결과에 있는지 확인하고, 없는 문장은 지운다"(근거 확인)
- "조회 결과에 **있는** 내용이면 기술적으로 상세하더라도 그대로 전달한다 — 문서에 적혀 있다는
  이유로 충분한 것이지, 스스로 걸러내지 마세요"(과잉 억제 방지)

**교훈.** "이건 LLM이 지어낸 것 같다"는 인상만으로 금지 규칙을 만들면 안 된다.
근거는 **매뉴얼 원문에서 확인**하고 나서 판단한다. 판단 기준은 "그럴듯한 일반 지식으로
보이는가"가 아니라 "조회 결과에 있는가" 하나뿐이다.

## 75. ssh 실패 메시지가 원인을 감추고 있었다 (완료)

job 조회가 `'login07'에 ssh 인증이 실패… 마운트된 키가 대상 서버에 등록돼 있지 않을 수
있습니다`로 실패했다. 그런데 같은 컨테이너에서 **같은 키로 202.20.185.100에 직접** 붙으면
성공한다(#69에서 확인). 차이는 하나뿐이다 — 우리 코드는 이름(`login07`)을 `/etc/hosts`로
IP로 바꿔 접속한다. **그 이름이 게이트 서버가 아닌 다른 IP로 풀리면** 키가 등록되지 않은
서버에 붙어 인증 실패가 난다.

그런데 기존 메시지는 **접속한 IP를 안 적었다.** 그래서 "키 문제"로 읽혔고, 실제로 나도
사용자도 키를 계속 의심했다. 메시지가 원인을 감춘 셈이다.
→ 모든 ssh 실패 메시지에 **해석된 IP**와 **ssh가 남긴 실제 사유 한 줄**을 넣었다.
키 파일이 정상인데 거부당한 경우에는 "이름이 다른 IP로 풀리는 경우가 가장 흔하다"로 안내한다.
255인데 위 두 갈래에 안 걸리는 접속 실패도 더 이상 조용히 넘기지 않는다.
→ `scripts/diag-ssh.sh` 추가: 이름 해석 결과 → 키 파일 상태 → 해석된 IP로 접속 →
게이트 IP로 직접 접속을 차례로 돌려, **3번은 실패하고 4번은 성공하면 이름 해석이 원인**임을
한 번에 판정한다.

## 76. 한 메뉴에 여러 가이드 문서 — `ppt_title`을 청크마다 저장 (완료)

**상황.** '활용 가이드' 메뉴 하나에 GPU 서버 활용 가이드, 계정 신청 가이드 … 여러 PPT가 들어
있다. 사용자는 매뉴얼 탭에 **메뉴까지만** 등록하고(`reference_path`), 개별 PPT 이름은 CSV의
**`ppt_title` 열**에 행마다 들어 있다. #74에서 만든 `reference_path`만으로는
"… > 활용 가이드"까지밖에 안내하지 못해 여전히 문서를 못 찾는다.

**조치.**
- `manual_chunks.doc_title` 추가(manual_db v8). tsv에도 포함해 "GPU 서버 활용 가이드" 같은
  질의가 **제목으로도** 잡히게 했다(+ doc_title 인덱스).
- 콘솔 엑셀/CSV 열 매핑에 **② 문서명** 열을 추가. `ppt_title`(및 문서명/자료명/document_title)이
  있으면 **자동 선택**된다.
- 임베딩·리랭커 입력을 `등록 제목 > 원본 문서 이름 > 섹션 제목`으로 확장(Contextual retrieval).
  한 메뉴 안 여러 문서가 등록 제목만으로는 구분되지 않던 문제를 없앤다.
- 검색 결과에 **`reference`** 를 미리 만들어 넣는다(`reference_path` + `doc_title`).
  LLM에게 두 필드를 조합시키면 빠뜨리거나 순서를 바꾸므로, 그대로 옮겨 적을 완성된 문자열을 준다.

**이웃 청크 확장의 문서 경계 처리(중요).** #73의 이웃 확장은 `seq`가 파일 전체에서 이어지므로,
**이웃이 다른 PPT의 첫/마지막 장일 수 있다.** 그대로 붙이면 계정 신청 가이드 뒤에 GPU 가이드가
이어진 '존재하지 않는 절차'가 만들어진다 → 이웃의 `doc_title`이 다르면 붙이지 않도록 막았다.
(seq 2가 A문서 마지막일 때 seq 3(B문서)이 안 붙는 것을 단위 테스트로 확인.)

**지시문.** "reference를 그대로 옮겨 적는다(조합 금지)" + 새 절 **"여러 가이드 문서가 함께
검색될 수 있습니다"**: doc_title이 다른 내용을 한 절차로 이어 붙이지 말고, 여러 문서를 인용해야
하면 문서별로 나눠 각각의 reference를 단다.

**반영 제약.** `doc_title`은 등록(커밋) 시점에 채워지므로 **기존 청크는 비어 있다.**
같은 CSV를 다시 업로드해 새 버전으로 올리고 발행해야 적용된다(NEXT-STEPS 4번).

## 77. `login07`이 75.11.29.7로 풀리고 있었다 — 이름 해석을 아예 버린다 (완료)

#75에서 ssh 실패 메시지에 **해석된 IP를 찍게** 했더니 바로 드러났다.
```
'login07'(75.11.29.7)에 ssh 인증이 실패해 커맨드가 실행되지 않았습니다.
```
게이트 서버는 **202.20.185.100**인데 배포 호스트 `/etc/hosts`의 `login07`은 전혀 다른 서버
(75.11.29.7)를 가리키고 있었고, 그 서버엔 우리 키가 없으니 **모든 커맨드 실행이 인증 실패**했다.
#69에서 손으로 IP를 넣은 테스트만 성공했던 이유가 이것이다.

**조치 — 이름 해석 자체를 타지 않게 한다.**
- `resolve_host()`: **IP 리터럴이면 `/etc/hosts` 조회를 건너뛰고 그대로 접속**한다
  (`ipaddress.ip_address()`로 판별). 이름은 종전대로 `/etc/hosts`에서 찾는다.
- 코드의 로그인 서버 기본값을 전부 `login05`/`login07` → **`202.20.185.100`** 으로 교체
  (migrations 시드, agent, command-mcp, system-mcp, compose 2종).
- **platform_config v3 마이그레이션**: 기존 DB의 `scheduler_login_host` 값이 IP 형식이 아니면
  `202.20.185.100`으로 강제 교체한다. 이미 IP면(=운영자가 정한 값) 건드리지 않는다.
  시드는 non-force라 기존 값을 못 덮으므로 마이그레이션이 필요했다.
- `CLAUDE.md`·`RUN-LOG.md`의 운영 설정값도 IP로 바꾸고 "이름 금지"를 명시.

**교훈.** 이름 해석은 우리가 통제하지 못하는 파일(배포 호스트 `/etc/hosts`)에 의존한다.
값이 하나 틀어지면 전 기능이 조용히 죽는데 증상은 "인증 실패"로 보여 엉뚱한 곳을 파게 된다.
고정 대상은 이름이 아니라 **IP로 박는다.**

## 78. 실행 결과까지 마스킹하던 문제 (완료)

`ls -l` 결과의 소유자 계정이 전부 `{사용자 id}`로 가려져 나왔다. 자기 파일 목록인데 누구 것인지
알 수 없는 출력이 된다.

원인은 지시문이다. #66의 개인정보 절이 "조회 결과에 사람이나 조직을 식별할 수 있는 값이 있으면"
으로 시작해 **커맨드 실행 결과까지 포함**되게 읽혔다. "본인 계정 실행 결과는 그대로 보여줘도
된다"는 예외를 한 줄 달아 뒀지만, 앞 문장이 더 강해서 묻혔다.

→ 절 제목부터 **"검색된 문서에만 적용됩니다"** 로 바꾸고, 마스킹 대상을 매뉴얼·VOC처럼
**남의 기록을 검색해 온 것**으로 한정했다. 그리고 별도 문단으로 못 박았다:
**커맨드 실행 결과는 절대 가리지 않는다** — 실행은 언제나 질문한 본인 권한으로만 이뤄지므로
거기 나오는 계정명·경로·파일명은 전부 본인 정보다.

## 79. 조건부 안내를 필수 절차로 바꿔 쓰던 문제 (완료)

매뉴얼 원문은 조건부다.
> Only for simulation workloads requiring CUDA compilation, you will need to recompile your
> CUDA binaries with "-arch=compute_80 -code=sm_80" flag.

그런데 답변은 `H100 (80GB HBM): CUDA 컴파일 시 -arch=compute_90 -code=sm_90 필요` 로 나왔다.
두 가지가 동시에 잘못됐다.
1. **조건이 사라졌다.** "시뮬레이션 작업 중 CUDA 컴파일이 필요한 경우에만"이 빠지고 필수 절차가 됐다.
2. **값이 바뀌었다.** 문서엔 `compute_80/sm_80`만 있는데 `compute_90/sm_90`을 지어냈다
   (H100용 값을 학습 지식에서 끌어와 GPU별 표처럼 확장했다).

→ 지시문 (A)에 두 항목 추가.
- **조건을 떼고 옮기지 않는다**: only/~인 경우에만/필요 시/권장 같은 말은 **반드시 함께** 옮긴다.
  잘못된 예·옳은 예를 실제 이 문장으로 넣어 뒀다.
- **문서에 적힌 값을 바꾸거나 일반화하지 않는다**: 플래그·숫자·경로·모델명은 원문 그대로.
  문서에 한 경우만 있으면 다른 경우를 유추해 만들지 않고 "문서에는 …만 나와 있습니다"라고 밝힌다.

#74에서 이걸 '환각'으로 오판해 컴파일 옵션 자체를 금지했다가 철회했는데, 진짜 문제는
**옵션을 쓴 것이 아니라 조건을 떼고 값을 바꾼 것**이었다. 규칙은 "쓰지 마라"가 아니라
"있는 그대로 옮겨라"가 맞다.

## 80. VOC를 업로드 묶음으로 관리 + 다중 선택 삭제 (완료)

CSV 하나를 올리면 수천 행이 개별 레코드로 들어가는데 콘솔에선 낱개로만 보여서,
"방금 올린 그 파일"을 되돌릴 방법이 없었다. 목록도 상한 200건에 페이지가 없어 2천 건을 볼 수 없었다.

- **voc_db v7**: `batch_id`/`source_file`/`uploaded_by` 추가. 업로드 한 번이 한 묶음이다
  (엑셀 자동인식 경로와 열 매핑 경로 둘 다 `uuid4`로 묶음을 만든다).
- API: `GET /api/voc/batches`(묶음 목록·건수), `DELETE /api/voc/batches/{id}`(묶음째 삭제),
  `POST /api/voc/bulk-delete`(선택 삭제). 목록은 `total`/`offset`/`limit` 페이지네이션 +
  `batch_id` 필터로 바꿨다(기존 응답이 배열 → 객체로 바뀌었으므로 프런트도 함께 수정).
- 콘솔 VOC 탭: 위에 **업로드 묶음** 표(이 묶음만 보기 / 묶음 삭제), 아래 목록에 **행별 체크박스 +
  페이지 전체 선택 + 선택 삭제**, 100건 단위 이전/다음, 출처 파일 열.
- 기존 데이터는 `batch_id`가 NULL이라 묶음 표에 안 나온다. 몇 건인지 안내 문구로 보여주고,
  목록에서 선택 삭제할 수 있게 했다.

## 81. TSV 업로드 지원 (완료)

VOC 데이터가 `.tsv`로도 들어온다. 표 읽기는 이미 공통 모듈(`spreadsheet.py`) 하나로 모여 있어서,
확장자만 열어 주면 매뉴얼·VOC·커맨드 탭이 **동시에** 지원된다.

- `TSV_EXTS = {".tsv"}` 추가 → `TABLE_EXTS`에 합류(세 라우터가 모두 이 상수를 쓴다).
- **`.tsv`는 구분자를 탭으로 고정한다.** CSV는 `csv.Sniffer`로 구분자를 추정하는데,
  확장자가 이미 탭을 명시하고 있으므로 추정에 맡길 이유가 없다. VOC 본문에는 쉼표가 흔해서
  추정이 어긋나면 열이 통째로 밀린다(추정이 대체로 맞긴 하지만, 맞을 때만 동작하는 코드를 둘 이유가 없다).
- 인코딩은 CSV와 동일하게 UTF-8(BOM)·CP949 등을 순서대로 시도한다.
- 콘솔 파일 선택창 `accept`에 `.tsv` 추가(매뉴얼/VOC/커맨드 3곳), 파일 종류 표시에 "TSV·탭 구분".

셀 안에 쉼표·줄바꿈이 든 실제 VOC 형태의 TSV(UTF-8/CP949)로 열 어긋남이 없는지 확인했다.

## 82. 등록 실패가 404로 덮이던 문제 — 세션을 실패에도 소모하고 있었다 (완료)

TSV 업로드에서 미리보기는 정상인데 "등록"을 누르면
`404 업로드 세션이 없거나 만료되었습니다. 다시 업로드하세요.` 가 떴다.

**원인.** 세 라우터(매뉴얼·VOC·커맨드)의 커밋이 모두 이렇게 돼 있었다.
```python
try:
    items = await run_in_threadpool(_build, session["saved_path"])
except ValueError as e:
    raise HTTPException(422, str(e))
finally:
    await delete_upload_session(...)   # ← 성공/실패 가리지 않고 삭제
```
`finally`라 **실패해도 세션이 사라진다.** 그래서 첫 시도가 어떤 이유로든 실패하면
(열 매핑 오류, 임베딩 서버 오류, 요청 타임아웃 등) 재시도는 무조건 404가 되고,
사용자가 보는 마지막 메시지는 **진짜 원인이 아니라 404**다. VOC는 2천 행을 한 행씩
임베딩하느라 몇 분이 걸려서, 중간에 끊길 여지가 특히 컸다.

**조치.**
- `finally` 제거 — **성공했을 때만** 세션을 지운다. 실패한 세션은 TTL(기본 60분)이 정리한다.
  이제 재시도가 되고, 무엇보다 **실패 사유가 그대로 보인다.** 세 라우터 모두 동일하게 고쳤다.
- VOC 삽입 루프에 예외 처리 추가: 임베딩이 도중에 끊기면
  "N건 중 M건을 넣은 뒤 중단했습니다 → 업로드 묶음에서 이번 묶음을 삭제하고 다시 등록하세요"로
  안내한다(#80의 묶음 삭제가 그대로 복구 수단이 된다).
- `import_voc_excel`에서 `read_upload_or_server_file`의 반환 순서를 잘못 받아
  (`(ext, content, filename)`인데 `filename, content, _`로 받음) `source_file`에 확장자가
  들어가던 것을 함께 고쳤다(#80에서 내가 넣은 실수).

**교훈.** 정리(cleanup)를 `finally`에 두면 **실패 경로까지 정리해 버려서** 다음 시도가
다른 오류로 바뀐다. 진단이 필요한 리소스는 실패 시 남겨 두는 편이 낫다.

## 83. 긴 행 하나가 일괄 등록 전체를 죽이고 있었다 (완료)

VOC 333행 중 **16행만 등록되고 500**. #82로 세션은 살렸지만 진짜 원인은 따로 있었다.

**원인.** `embed_text()`에 **입력 길이 제한이 없었다.**
매뉴얼 청크는 1500자로 잘라 두지만 VOC는 원문 한 건(의뢰내용 + 처리내용)이 그대로 들어간다.
bge-m3의 한도는 8192토큰이라, 긴 문의 한 건에서 임베딩 서버가 400을 돌려주고
`raise_for_status()`가 예외를 던져 **그 뒤 300여 건이 통째로 날아갔다.**
17번째 행 하나 때문에 나머지 전부를 잃은 것이다.

**조치 세 가지.**
1. **입력 길이 상한**(`embed_max_chars`, 기본 4000자). 넘으면 잘라서 보낸다.
   한글은 토크나이저에서 글자당 대략 1~1.5토큰이라 4000자면 한도 안쪽이다.
   0으로 두면 자르지 않는다(한도가 더 큰 모델을 쓸 때).
2. **임베딩 서버 오류 메시지를 그대로 올린다.** 예전에는 `raise_for_status()`라
   "400 Bad Request"만 보이고 *왜* 거절됐는지(길이 초과인지 모델명 오류인지)를 알 수 없었다.
   이제 응답 본문을 붙여서 던진다.
3. **행 단위 내성**(`_insert_many`). 한 행의 임베딩이 실패해도 **그 행은 임베딩 없이 저장하고
   계속 진행**한다 — 벡터 검색에서만 빠지고 키워드·3gram 축에는 잡히므로 데이터를 잃지 않는다.
   다만 임베딩 서버 자체가 죽었을 때 수백 건을 그대로 밀어 넣으면 안 되므로,
   **5건 연속 실패면 중단**하고 몇 건까지 들어갔는지와 복구 방법을 알려 준다.
   레거시 임포트 경로(사내 표준 4행 헤더 / 1행 Question·Answer)도 같은 함수를 쓰게 바꿨다.
   등록 결과에 `embed_failed`/`embed_errors`를 담아 콘솔이 "몇 행이 왜 실패했는지"를 보여준다.

**테스트 중 잡은 버그.** `_insert_voc(..., embedding=vec)`에서 `vec`가 `None`(임베딩 실패)일 때
`embedding is not None`으로 판정해 **함수 안에서 임베딩을 다시 호출**, 같은 예외가 또 났다.
`_NO_EMBEDDING` 표식을 둬 "안 넘긴 것"과 "만들지 못한 것"을 구분했다.
단위 테스트(한 건만 실패 / 서버 전체 다운)로 두 경로를 확인했다.

**교훈.** 외부 모델 호출은 **입력 크기 상한이 있고**, 일괄 작업은 **한 건의 실패가 전체를
죽이지 않아야** 한다. 둘 다 없으면 데이터 하나가 파이프라인 전체를 멈춘다.

## 84. `field larger than field limit (131072)` — CSV 한 칸이 128KB를 넘었다 (완료)

파이썬 `csv` 모듈의 필드 크기 기본 상한이 **128KB**다. VOC 처리내용처럼 긴 본문이 한 칸에
들어가면 이 한도를 넘어 **파일 전체를 읽지 못한다.**

**조치.**
- `csv.field_size_limit()`을 **64MB**로 올린다(업로드 자체가 `upload_max_mb` 기본 50MB로
  제한되므로 그보다 넉넉한 값). 플랫폼에 따라 C long을 넘으면 `OverflowError`가 나므로
  절반씩 낮추며 시도하는 헬퍼를 뒀다.
- 그래도 걸리는 경우(따옴표 짝이 안 맞아 여러 행이 한 칸으로 붙는 등)를 위해
  `csv.Error`를 사람이 읽을 수 있는 안내로 바꿔 던진다 — 인코딩 문제가 아니라 형식 문제이므로
  다른 인코딩으로 재시도하지 않고 즉시 알린다.
- 132,000자짜리 셀이 든 CSV로 읽기·열 매핑까지 확인했다.

**딸려 나오는 문제 하나 더 — 그 긴 본문이 그대로 에이전트로 간다.**
VOC 검색은 question/answer를 자르지 않고 반환하고 있었다. 수십만 자짜리 사례가 한 건만 걸려도
리랭커 입력 한도를 넘기고, LLM 컨텍스트(32768토큰)는 몇 건 만에 가득 찬다.
→ `voc_result_max_chars`(기본 2000자)로 **리랭커 입력과 반환값 모두** 앞부분만 쓴다.
원문은 DB에 그대로 남는다.

## 85. 임베딩을 묶어서 호출 — "등록 눌러도 아무 일도 안 일어난다" (완료)

큰 CSV에서 등록 버튼을 눌러도 화면에 아무 변화가 없다는 리포트.
멈춘 게 아니라 **아직 돌고 있었다.** 일괄 등록이 행마다 `embed_text()`를 부르고 있어서
2천 행이면 HTTP 왕복이 **2천 번**이다. 몇 분이 걸리는 동안 버튼만 비활성화될 뿐
진행 상황이 보이지 않으니 "아무 일도 안 일어난다"로 보인다.

**조치.**
- `shared/db.embed_texts()` 추가 — OpenAI 호환 임베딩 API는 `input`에 **배열**을 받는다.
  32건씩 묶어 보내면 2천 행이 요청 **63회**로 준다(단위 테스트로 100건 → 4회 확인).
- 한 배치가 통째로 실패하면(입력 하나가 유난히 길거나 형식이 이상한 경우) **그 배치만
  한 건씩 재시도**해 문제 행만 골라낸다. 나머지는 그대로 살린다.
- 서버 자체가 죽었을 때 수천 번 두드리지 않도록 **연속 실패 5건이면 중단**한다.
- 캐시(Redis)도 배치 경로에서 그대로 쓴다 — 이미 임베딩된 행은 요청에서 빠진다.
- 응답의 `index` 필드를 우선 신뢰하되 없으면 순서로 맞춘다(서버 구현 차이 대비).
- 콘솔: 등록 중일 때 "N행을 임베딩하고 저장하는 중이니 창을 닫지 마세요" 안내를 띄운다.

## 86. VOC에서 부서·해결여부·제외기준 열 제거 (완료)

실제로 쓰이지 않는 항목이라 화면·API에서 뺐다(사용자 요청).

- 콘솔: 개별 등록 폼의 부서 입력·"해결됨" 체크박스, 열 매핑의 **부서 열**과
  **제외 기준 열/제외 값**, 목록 표의 부서·해결 열을 제거.
  ("비어 있으면 건너뛸 열"은 성격이 다른 기능이라 남겼다.)
- API: `VocIn`에서 department/resolved 제거, `VocTableCommitIn`에서 department_column·
  exclude_column·exclude_values 제거, 미리보기 제안(`suggest`)에서도 뺐다.
- **VOC MCP의 `search_voc`에서도 `department`/`resolved_only` 파라미터를 제거했다.**
  이게 중요한데, 더 이상 채우지 않는 컬럼으로 LLM이 필터를 걸면 **결과가 0건이 된다.**
  입력에서 뺐으면 검색에서도 빼야 한다. 반환 항목에서도 두 필드를 뺐다.
- DB 컬럼(`department`, `resolved`)은 기존 데이터를 위해 남겨 둔다 — 새 등록은 기본값.

## 87. job 커맨드가 코드에 박혀 있어 콘솔 수정이 안 먹었다 (완료)

"내 job 리스트" 질문에 `phd info` **사용법 오류**가 났고, 사용자가 콘솔 커맨드 탭에서 고쳤는데도
반영되지 않았다.

**원인.** `get_scheduler_job_info`가 `["phd", "info", "-u", user_id]`를 **코드에 하드코딩**하고
있었다. 커맨드 카탈로그를 아무리 고쳐도 이 툴은 쳐다보지 않는다.
(카탈로그를 쓰는 `run_command`는 DB를 매번 읽으므로 즉시 반영되는 게 맞다 — 그래서
"바로바로 반영되는 거 아니냐"는 물음이 정당했고, 이 툴만 예외였다.)

**조치.** 설정 키 `scheduler_job_command`(기본 `phd info -u {user_id}`, hot_reload)로 빼고
매 호출마다 읽는다. `{user_id}`는 호출자 계정으로 치환되며, 기존 `build_catalog_argv`를
그대로 타므로 셸 미사용·파괴적 명령 거부가 동일하게 적용된다. 사이트마다 스케줄러 사용법이
다른데 코드에 박아 둘 이유가 없었다.

## 88. 실행하지 않고 결과를 지어내고 있었다 (완료)

"내 홈 파일 리스트 보여줘"에 대한 출력이 **지난 리포트와 글자 하나 틀리지 않고 같았다**
(`documents / projects / data / README.txt`, 날짜까지 동일). 진짜 실행 결과라면 그럴 수 없다.
= **도구를 호출하지 않고 그럴듯한 `ls -l` 출력을 만들어낸 것.**

계정이 `{사용자 id}`로 나온 것도 이걸로 설명된다. #78에서 "실행 결과는 가리지 말라"고 고쳤는데도
그대로였던 이유는, 애초에 **실행 결과가 아니라 창작물**이었기 때문이다. 마스킹 규칙을 자기
창작물에 적용한 것이다. 엉뚱한 곳(마스킹 규칙)을 두 번 고칠 뻔했다.

→ 지시문 답변 방식에 명시: **실행 결과를 지어내지 않는다.** 도구를 호출하지 않았으면 출력은
존재하지 않으며, 그럴듯한 예시 출력(파일 목록·용량 수치·job 목록)을 만들어 보여주는 것은
거짓 정보다. 실행하지 못했으면 못했다고 말한다.

**판별법(사용자에게 안내).** 진행 상황 블록에 `· 'ls -l' 실행하는 중` 줄이 있는지 보면 된다.
실행 줄 없이 출력만 있으면 창작이다. 이 블록이 그대로 진위 판별기가 된다.

## 89. 주제를 바꿔 물으면 앞 답변이 섞인다 (완료)

"gpu 사용법" 다음에 "cpu 사용법"을 물으면 GPU 내용이 섞여 나온다.
검색이 틀렸다기보다 **대화 맥락에 남은 앞 답변을 근거로 재사용**하는 문제다.

→ 지시문에 절을 하나 추가했다: **질문이 바뀌면 처음부터 다시 검색한다.**
- 새 질문은 그 질문만으로 다시 검색하고, 이번에 조회한 내용만으로 답을 만든다.
- 대상만 바뀐 질문(GPU→CPU)이 가장 위험하다. 앞 답변의 문장·수치·옵션을 옮기지 않는다.
- **대화 맥락은 "무엇을 묻는지 이해하는 데"만 쓰고, 근거로는 쓰지 않는다.**

`rerank_min_score`를 올리면 관련도 낮은 문서를 더 세게 거를 수 있지만(사용자 측정:
같은 주제 0.699 / gpu↔cpu 0.217), **기본값은 0.05로 두었다.** 지금 `myquota`가 검색되지
않는 문제(#90)와 방향이 반대라, 재현율을 떨어뜨리면 그쪽이 악화된다.
필요하면 콘솔에서 0.2로 올리도록 안내만 남겼다.

## 90. `myquota`가 매뉴얼에서 계속 안 잡힘 (진행 중)

"내 홈 스토리지 용량"에 "확인할 수 있는 커맨드가 없습니다"가 계속 나온다.
매뉴얼에 올려 뒀다는데 검색이 못 찾는다.

**지금까지 한 것.** 지시문에 **매뉴얼 조회를 강제**했다 — 커맨드 카탈로그에 없어도
"커맨드가 없다"고 답하기 **전에 반드시 매뉴얼을 검색**하고, 표현을 바꿔 한 번 더 시도한 뒤에만
그렇게 답한다. 매뉴얼에서 찾은 커맨드는 등록 여부와 무관하게 그대로 실행한다.

**아직 못 밝힌 것.** 매뉴얼에 실제로 있는데 검색이 못 찾는 것인지, 아니면 그 문서가
발행(published)되지 않았거나 임베딩이 비어 있는 것인지 구분이 안 된다. 서버 데이터를 볼 수
없으므로 콘솔 **매뉴얼 탭 검색 테스트**에 "내 홈 스토리지 용량"을 넣고 결과 화면
(`mode`/`embedding`/`published_chunks`/`min_score`/각 결과 점수)을 받아야 판정할 수 있다.
검색 테스트는 #73에서 챗봇과 같은 코드로 통일했으므로, 거기 결과가 곧 챗봇 결과다.

## 91. 마스킹 정규식이 문장 내용을 삭제하고 있었다 (완료)

"방화벽 관련 VOC" 답변이 이렇게 깨져 나왔다.
```
DX부문 및 해외 사용자:  통한 방화벽 신청     ← "OO을"이 통째로 사라짐
```
`shared/pii.py`의 조직명 정규식 두 곳이 잘못돼 있었다.

**(1) 앞부분이 공백을 넘어 문장을 먹었다.**
`[A-Za-z가-힣0-9][A-Za-z가-힣0-9\s]{0,15}?(사업부|…|팀)` — 프리픽스에 `\s`가 들어 있어
**단어 경계를 넘어** 앞 문장을 조직명의 일부로 삼켰다.
```
"플랫폼팀이 처리했고 인프라팀은 확인만" → "{팀명}{팀명}은 확인만"   ("이 처리했고"가 삭제)
```
→ 프리픽스에서 공백을 제거했다. 조직명은 대개 한 토큰이다("메모리사업부", "DS부문").
띄어쓴 조직명("AI 개발팀")은 뒷 토큰만 가려지지만, 문장을 깨뜨리는 것보다 낫다.

**(2) `\b`가 한국어에서 동작하지 않았다.**
접미사 뒤에 `\b`를 요구했는데, 한국어 조사는 한글이라 단어 문자로 취급돼 경계가 생기지 않는다.
그래서 `DS부문 `(공백)은 가려지고 `DX부문은`(조사)은 **그대로 노출**됐다 —
개인정보는 새고 문장은 깨지는 최악의 조합이었다.
→ 조사를 명시적으로 허용하는 lookahead로 바꿨다. "팀장"처럼 다른 단어로 이어지는 경우는 제외.

수정 후 확인: 두 부문 모두 일관되게 마스킹, "처리했고"·"Knox Portal을 통한" 등 본문 보존,
`팀장`·`모듈러`·`nvidia-smi`·`hgpu4041` 오탐 없음.

## 92. 없는 문서를 지어내 안내하던 문제 (완료)

VOC 답변 끝에 "슈퍼컴 포탈의 방화벽 관련 가이드를 참고하시기 바랍니다"가 붙었는데
**그런 문서는 없다.** 게다가 `{부문명}`으로 가려 둔 값을 "DS부문"·"DX부문"으로 **되메워** 썼다.

→ 지시문 두 곳을 고쳤다.
- 문서 안내 절 제목을 **"존재하는 문서만 안내합니다"** 로 바꾸고: 문서를 언급하려면 **먼저
  매뉴얼을 검색해 결과에 실제로 있어야** 한다. 없으면 **안내 문장 자체를 쓰지 않는다**
  ("관련 가이드를 참고하시기 바랍니다" 같은 막연한 마무리 금지). VOC로 답한 뒤에도 동일.
- 자리표시자 절: `{부문명}`을 실제 이름으로 바꿔 쓰면 안 된다 — 학습 지식으로 메운 값이라
  틀릴 뿐 아니라, **가리려고 지운 정보를 되살리는 것**이 된다.

## 93. 큰 파일 등록·발행이 멈춘 것처럼 보이던 문제 — 모든 탭에 배치 임베딩 (완료)

"큰 파일 등록하니까 발행 버튼이 안 눌린다." VOC(#85)와 **똑같은 원인**이 매뉴얼·커맨드 탭에도
남아 있었다. 청크/행마다 `embed_text()`를 부르니 수천 행이면 HTTP 왕복도 수천 번이고,
그동안 화면에는 아무 표시가 없다.

**세 곳 모두 `embed_texts()`(32건 묶음)로 바꿨다.**
- **매뉴얼 발행**: 여기에 더해 **창(window) 단위**로 처리한다. 1만 청크의 벡터를 한꺼번에
  메모리에 들고 있으면 수백 MB가 되므로, 200개씩 "임베딩 → 저장"을 반복해 메모리를 일정하게
  유지한다. 중간에 끊겨도 이미 임베딩된 청크는 `embedding IS NULL` 조건에서 빠지므로
  **다시 발행을 누르면 이어서** 진행된다.
- **매뉴얼 재임베딩**: 배치 + 실패 건수 반환. 실패만 남았을 때 무한 반복하지 않도록
  프런트 루프에 중단 조건을 넣었다.
- **커맨드 일괄 등록**: 배치. 임베딩이 통째로 실패해도 `embedding=NULL`로 저장해
  (키워드·3gram 검색에는 잡힌다) 등록 자체가 실패하지 않게 했다.

**실패 내성 원칙을 통일했다.** 한 건의 임베딩 실패로 전체가 죽지 않는다 — 그 항목만
임베딩 없이 저장하고 건수를 알린다. 임베딩 서버가 죽은 경우만 중단한다(연속 5건 실패).

**콘솔 피드백.** 발행 버튼이 "발행 중… (청크가 많으면 몇 분 걸립니다)"로 바뀌고, 끝나면
임베딩 개수와 실패 건수를 알린다. 진행 표시가 없던 것이 "버튼이 안 눌린다"의 실체였다.

## 94. job 조회 전용 툴·설정 키를 없앴다 — 커맨드는 카탈로그가 유일한 출처 (완료)

#87에서 하드코딩된 `phd info -u <user>`를 설정 키 `scheduler_job_command`로 뺐는데,
**그것도 틀린 답이었다.** 사용자 지적: *"이렇게 하는 게 아니라 command_mcp를 호출해서 찾아서
실행해야지. 관리자 콘솔에서 command db 로 저장했다니까."*

맞는 말이다. 커맨드를 고칠 곳이 **커맨드 탭 카탈로그**와 **설정 탭 키** 두 군데가 되면,
"어디를 고쳐야 반영되나"가 갈려서 오히려 #87 이전보다 나빠진다. 코드 → 설정으로 옮긴 건
하드코딩을 한 겹 미룬 것일 뿐, 출처를 하나로 만든 게 아니었다.

**조치.**
- `get_scheduler_job_info` **툴 자체를 삭제**했다. 이제 Command MCP의 실행 툴은 `run_command`
  하나뿐이고, 에이전트는 `search_commands`로 카탈로그에서 찾아 실행한다.
  카탈로그의 `{user_id}`는 기존대로 호출자 계정으로 치환된다.
- 설정 키 `scheduler_job_command`를 시드에서 빼고, **platform_config v4 마이그레이션으로
  기존 DB에서도 DELETE** 한다(설정 탭에서 사라진다).
- `run_command` 설명에 "'내 job 목록'·'내 작업 상태'·'내 스토리지 용량' 같은 본인 자원 확인도
  전부 이 툴로 처리한다"를 명시.
- 지시문 "확인해 달라" 절에 **"스케줄러 job 조회도 예외가 아니다. 전용 툴은 없다"**를 넣고,
  `phd info` 같은 커맨드를 기억으로 쓰지 말고 검색 결과에 나온 것을 그대로 쓰도록 했다.

`scheduler_login_host`(ssh 접속 대상 IP)는 커맨드가 아니라 인프라 주소이므로 설정에 남는다.

**교훈.** "설정으로 빼면 유연해진다"가 항상 옳지는 않다. 이미 그 데이터의 주인이 있는 곳
(여기서는 커맨드 카탈로그)이 있으면, 설정 키를 하나 더 만드는 건 **출처를 둘로 쪼개는 것**이다.

**딸려 나온 것 — 회귀 테스트가 계속 실패하고 있었다.**
`test_system_mcp_tool_schema_preserves_params`가 system_mcp에 **있지도 않은**
`get_scheduler_job_info`를 보고 있어서 이번 변경 이전부터 실패 중이었다(확인함).
게다가 단언이 거꾸로였다 — "user_id가 스키마에 노출되어야 함"이라고 돼 있는데,
실제 설계는 **user_id를 감추고 호출자 헤더에서 강제 주입**하는 것이다(user_scoped=True).
실제 툴(`gpu_status`, `list_dir`)로 바꾸고 단언을 현재 설계대로 고쳤다:
실제 파라미터는 보이고, `kwargs`로 뭉개지지 않으며, `user_id`는 노출되지 않는다.
로그인 서버 고정 툴은 `host`까지 감춘다.

그리고 이번 결정을 지키는 테스트를 새로 넣었다 —
`test_command_mcp_has_no_hardcoded_command_tool`: Command MCP의 실행 툴은
`run_command` 하나뿐이어야 한다. 특정 커맨드를 박아 둔 전용 툴이 다시 생기면 실패한다.
(전체 19개 통과)

## 95. ContextWindowExceededError — 도구 결과와 대화 이력에 예산을 뒀다 (완료)

```
This model's maximum context length is 32768 tokens.
However, your request has 33125 input tokens.   ("gpu 사용법 알려줘")
                          35252 input tokens.   ("계정 신청은 어떻게 해? 방화벽은…")
```
검색 품질을 올리려고 넣은 것들이 **하나씩은 옳지만 합치면 컨텍스트를 넘겼다.**
- 매뉴얼 결과: 이웃 청크 확장(#73)까지 붙어 한 건이 최대 ~4700자 × top_k 5
- VOC 결과: 한 건 2000자 × 5
- 대화 이력: Open WebUI가 **대화 전체**를 매 요청에 실어 보낸다(상한 없었음).
  검색 결과가 붙은 긴 답변이 몇 번 쌓이면 이력만으로 컨텍스트가 찬다.
- 지시문 자체도 7천 자를 넘어섰다.

**조치 — 세 곳에 상한을 뒀다(전부 설정으로 조정 가능).**
- `manual_result_max_chars`(1500): 이웃 청크를 붙인 **뒤에** 자른다. 잘린 자리에
  "더 필요하면 get_document"를 남겨 필요하면 이어 읽을 수 있게 했다.
- `voc_result_max_chars`: 2000 → **1500**
- `history_max_chars`(8000): `_trim_history()`가 **오래된 턴부터** 버린다.
  최근 턴이 가장 중요하므로 뒤에서부터 채우고, 한 턴이 예산보다 커도 **최소 1턴은 남긴다**
  (질문 자체가 사라지면 안 되므로). 단위 테스트로 확인.

대략 계산: 지시문 ~4.5k + 매뉴얼 5×1500 ~5k + VOC 5×1500 ~5k + 이력 ~5k ≈ 20k 토큰.

**교훈.** 검색 품질 개선(이웃 확장·top_k·문맥 주입)은 전부 컨텍스트를 먹는다.
개별로는 타당해도 **총량 예산이 없으면 어느 순간 한계를 넘는다.** 상한을 먼저 정했어야 했다.

## 96. 실행 결과에 '어디서 누구로' 돌았는지 표시 (완료)

`ls -l` 출력이 나왔지만 "202.20.185.100에서 내 계정으로 실행한 게 맞느냐"를 확인할 방법이
없었다. 결과 dict에는 `ip`/`as_user`가 이미 들어 있는데 화면에는 안 보였다.

→ 진행 상황 줄에 붙였다: `· 완료 (202.20.185.100 · yr9.choi)`.
실패도 `· 실패 (75.11.29.7 · yr9.choi) — ssh 인증이 실패…`처럼 IP가 보인다.
#77에서 에러 메시지에 IP를 넣자마자 원인이 드러난 것과 같은 이유로, **정상 경로에도**
어디서 돌았는지 보여야 한다.

## 97. TSV는 매뉴얼 탭에서도 이미 된다 (확인)

"csv가 콤마로 구분되어 있어서 임베딩이 잘못 들어간 것 같다, tsv로 올릴 수 있게 해달라" →
**#81에서 이미 세 탭 모두 지원하도록 넣었다**(표 읽기가 `spreadsheet.py` 하나로 모여 있어
확장자만 열면 매뉴얼·VOC·커맨드가 함께 열린다). 매뉴얼 탭 파일 선택창에도 `.tsv`가 있고,
`.tsv`는 구분자를 탭으로 고정해 본문에 쉼표가 있어도 열이 어긋나지 않는다.
아직 배포가 안 됐을 뿐이라, NEXT-STEPS에 "TSV로 다시 업로드" 단계로 넣었다.

## 98. Q3이 왜 갑자기 되나 — 카탈로그가 유일한 출처가 됐기 때문 (설명)

"이거 이제 실행은 되네. 어떻게 한 거야?" → #94에서 **`get_scheduler_job_info` 툴을 삭제**했다.
그전에는 그 툴이 `phd info -u <user>`를 코드(그 다음엔 설정값)에서 읽어 실행했기 때문에,
콘솔 커맨드 탭에서 아무리 고쳐도 반영되지 않았다.

지금은 job 조회도 다른 커맨드와 똑같은 경로를 탄다:
`search_commands`(커맨드 카탈로그 = command_db 관계형 테이블) → `run_command`.
**관리자가 콘솔에서 저장한 그 행이 그대로 실행된다.** 그래서 사용자가 고쳐 둔 사용법이
비로소 먹은 것이다.

## 99. 커맨드 실행 속도 — 연결 다중화 + 탐색 금지 (완료)

"커맨드들은 무조건 다 빠르게 실행해서 뱉어야 한다."
느린 이유는 커맨드 자체가 아니라 **호출 횟수 × 접속 비용**이었다.

**(1) 접속 비용 — ssh 연결 다중화(ControlMaster).**
커맨드 하나마다 TCP 핸드셰이크 + SSH 키 교환 + 원격 `su -` 로그인 셸을 새로 열고 있었다
(요청당 1~3초). `ControlMaster=auto` + `ControlPersist=300`으로 **첫 연결만 실제로 맺고
이후 커맨드는 같은 연결에 채널만 얹는다.** 두 번째부터 핸드셰이크가 사라진다.
- 소켓 경로는 `%C`(호스트·포트·사용자 해시)로 만들어 유닉스 소켓 경로 길이 제한을 피한다.
- 소켓 디렉토리는 0700으로 만들고, 만들 수 없으면 **다중화 없이 그대로 동작**한다.
- `.env`의 `SSH_MULTIPLEX=false`로 끌 수 있다.

**(2) 호출 횟수 — 탐색을 막았다.**
실제 로그가 이랬다: `list_dir` → `list_dir` → `echo $HOME` → `pwd` → `list_dir`.
홈 경로를 알아내려고 확인용 커맨드를 앞세우고, 같은 도구를 반복 호출했다.
접속이 5번이면 사용자는 5번의 지연을 그대로 기다린다.
→ 지시문에 절을 추가했다: **커맨드는 한 번에 실행한다.**
`echo $HOME`·`pwd`·`whoami` 같은 확인용 커맨드를 앞세우지 않는다(실행은 **항상 본인 홈에서
시작**하므로 경로를 비우거나 `.`이면 홈이다), 같은 도구를 같은 인자로 두 번 부르지 않는다
(결과가 비어 있어도 그건 "비어 있다"는 답이지 오류가 아니다).

## 100. 진행 상황의 `<details>` 래퍼 제거 (완료)

클라이언트에 따라 `<details open> <summary> 진행 상황 </summary>`가 **태그째 그대로** 보였다.
#62에서 Open WebUI가 `<think>`를 접어버려 `<details>`로 바꿨던 건데, 이제는 그 태그가
노출되는 쪽이 더 나빴다.
→ 래퍼를 걷어내고 진행 줄만 그대로 흘린다. 답변이 시작되면 한 줄만 띄운다.

## 101. 참고 문서 목록에도 경로를 붙인다 (완료)

답변 본문에서는 경로가 나오는데, 맨 끝 **참고 문서 목록**에서는 이름만 나열하고 있었다.
→ 지시문에 명시: 목록에서도 한 줄에 하나씩 **reference를 그대로** 적는다.
reference가 있는데 이름만 쓰면 안 된다.

## 102. 계정 신청 질문에 GPU 가이드가 나옴 (미해결)

"계정 신청은 어떻게 해? 방화벽은 뭐 신청해야해?"에 **GPU 서버 활용 가이드**가 참고문서로
붙었다. 검색이 엉뚱한 문서를 집어오고 있다.

아직 원인을 못 가렸다. 후보는 (a) 계정/방화벽 가이드가 발행되지 않았거나 임베딩이 비어 있다,
(b) 임베딩은 됐는데 CSV 파싱이 어긋나 청크 내용이 뒤섞였다(사용자가 의심하는 쪽),
(c) 검색은 맞는데 리랭킹에서 밀렸다. 서버 데이터를 볼 수 없으므로 콘솔 **매뉴얼 탭 검색
테스트**에 "계정 신청"·"방화벽 신청"을 넣고 결과 화면(mode/embedding/published_chunks/
min_score/각 점수)을 받아야 판정할 수 있다. TSV 재업로드(NEXT-STEPS 3번)가 (b)를 먼저 지운다.

## 103. 접속 시점 정리 + 연결 예열 (완료)

질문: *"사용자가 agent에 접속하면 바로 202.20.185.100 로그인 셸에 user id로 ssh 접속하게
되는 거지?"*

**실제 동작(수정 전).** 채팅에 접속하는 시점에는 아무 접속도 하지 않는다. 커맨드를 실행할
때마다 `ssh root@202.20.185.100` → `su - <user_id> -c <커맨드>`를 새로 연다. 즉
**"사용자별 로그인 세션"이 상주하는 게 아니라, 커맨드마다 그 사용자 권한으로 접속**한다.

**사용자별 상주 셸로 바꾸지 않은 이유.** CLAUDE.md §3의 절대 규칙은 "모든 실행은
`run_ssh_as_user()` 하나만 거친다"이다. 사용자별로 셸을 띄워 두면 (a) 그 셸에 상태가 남아
요청 간에 새어 나가고, (b) 실행 경로가 하나 더 생겨 우회로가 되며, (c) 유휴 셸/좀비 프로세스
관리가 새 문제로 들어온다. 얻는 건 결국 **접속 지연 제거**뿐인데, 그건 아래로 해결된다.

**대신 한 것 — 연결을 미리 열어 둔다.**
- `warm_master(host)`: ssh 마스터 연결만 여는 함수(`su` 없이 `true` 한 번).
- Command MCP·System MCP가 **기동 시** 로그인 서버로 마스터를 열고,
  `start_master_keepalive()`가 240초마다 다시 예열해 `ControlPersist=300`이 끊기지 않게 한다.
- 결과: 사용자가 첫 질문을 던지는 시점에 이미 연결이 서 있고, 실제 커맨드는 그 위에 채널만
  얹는다. #99의 다중화가 "두 번째 커맨드부터" 빨라지게 했다면, 이건 **첫 커맨드까지** 빠르게 한다.
- 예열이 실패해도 서비스를 막지 않는다(다음 커맨드가 평소대로 직접 접속). 이름이 안 풀리면
  ssh를 아예 호출하지 않고 로그만 남긴다. 단위 테스트로 argv·실패 경로·주기를 확인했다.

`_base_ssh_opts()`로 옵션 구성을 한 곳에 모아, 예열과 실제 실행이 **같은 ControlPath**를
쓰도록 보장했다(다르면 예열이 무의미해진다).

## 104. Command MCP는 RAG, System MCP는 RAG 아님 (정리)

질문: *"command랑 system mcp는 여전히 rag 방법을 쓰는 거야?"* — 둘은 다르다.

| | 검색 방식 | 무엇을 고르나 |
|---|---|---|
| **Command MCP** | **RAG 씀.** `search_commands`가 3축 RRF(벡터+키워드+3gram) → 리랭킹 → MMR | `command_catalog` 테이블의 행(관리자가 콘솔 커맨드 탭에서 등록·수정) |
| **System MCP** | **RAG 안 씀.** 검색 코드가 아예 없다 | 코드 화이트리스트 + 콘솔 등록 커스텀 커맨드가 **MCP 툴 목록 그 자체**로 노출되고, LLM이 툴 설명을 보고 고른다 |

즉 System MCP는 "툴이 몇 개 있고 각각 뭘 하는지"를 LLM이 직접 보고 고르는 구조라 검색이
필요 없다. 반대로 Command MCP는 카탈로그가 수백~수천 행이 될 수 있어 전부 툴로 노출할 수
없으므로 검색이 필요하다. 그래서 #94에서 job 조회를 Command MCP 쪽(카탈로그+검색)으로
일원화한 것이 맞는 방향이었다 — 커맨드가 늘어나도 툴 목록이 부풀지 않는다.

## 105. Command MCP도 RAG를 걷어내고 **카탈로그를 툴로 노출** (완료)

사용자 결정: *"command mcp, system mcp 둘 다 동일하게 rag 안 쓰는 걸로 하자.
tool로 노출하고 agent가 고르게 하자."*

**왜 맞는 방향인가.** `search_commands`가 한 번 어긋나면 등록해 둔 커맨드가 **통째로 없는 것처럼**
취급된다("내 홈 스토리지 용량"이 `myquota`를 못 찾는 일이 반복됐다). 툴로 노출하면 LLM이
목록을 직접 보고 고르므로 그 실패 모드 자체가 사라진다. #104에서 정리했듯 System MCP는
이미 그 방식이라 두 MCP의 동작도 하나로 맞춰진다.

**구현.** `mcp_servers/command_mcp/catalog_tools.py` 신설 — System MCP의
`custom_whitelist.load_custom_whitelist_sync()`와 같은 패턴으로 `command_catalog` 행을
읽어 툴 하나씩 만든다. 실행 경로는 그대로다(`build_catalog_argv` → `run_ssh_as_user`).
`search_commands`·`get_command_detail`은 삭제했다. `run_command`는 남긴다 —
**매뉴얼 문서에서 찾은 미등록 커맨드**를 실행하는 용도(사용자가 예전에 못 박은 요구사항).

**툴 이름 짓기에서 두 번 걸렸다.**
1. 카탈로그 이름이 한글이면 ASCII만 남겼을 때 아무것도 안 남아 **전부 `cmd_c`로 뭉개졌다.**
   OpenAI 호환 함수 이름 규칙(`[a-zA-Z0-9_-]{1,64}`) 때문에 한글을 그대로 쓸 수도 없다.
   → 이름이 비면 **실행 커맨드에서** 만든다(`phd info -u {user_id}` → `cmd_phd_info`).
2. 그래도 안 되면 해시를 붙이는데, 처음엔 파이썬 `hash()`를 썼다. 이건 프로세스마다 값이
   달라져(PYTHONHASHSEED) **재시작할 때마다 툴 이름이 바뀐다.** → sha1 기반 고정 해시로 교체.
   두 경우 다 회귀 테스트로 묶었다(ASCII 규칙·중복 없음·재호출 시 동일).

**트레이드오프(알고 택한 것) — 사용자에게 알려야 한다.**
- 카탈로그를 고치면 **command-mcp 재시작이 필요하다.** 검색 방식은 매번 DB를 읽어 즉시
  반영됐다. 이건 명백한 후퇴지만, "고쳐도 반영이 안 된다"의 원인이던 하드코딩과 달리
  재시작하면 확실히 반영되고 원인도 명확하다.
- 툴 설명이 전부 프롬프트에 실리므로 카탈로그가 크면 컨텍스트를 먹는다.
  상한 `command_tools_max`(기본 200)를 두고, 넘치면 경고를 찍고 남는 커맨드는
  `run_command`로만 실행되게 했다. #95에서 컨텍스트 한계에 부딪힌 직후라 상한 없이 갈 수 없었다.

**"skill로 등록할 수는 없어?"** — 이 스택(Google ADK + vLLM + MCP)에는 별도의 skill 등록
메커니즘이 없다. **MCP 툴이 그 역할을 한다** — 이름·설명·파라미터 스키마를 붙여 등록하면
에이전트가 목록에서 골라 쓰는, 지금 한 것이 정확히 그 방식이다.

## 106. "skill로 등록하면 프롬프트에 안 실리나" — 아니다 (설명)

**이 스택에는 skill 메커니즘 자체가 없다.** Google ADK + vLLM(Qwen) + MCP 조합에서 에이전트가
쓸 수 있는 등록 수단은 **MCP 툴 하나뿐**이다.

그리고 skill이 있는 환경(Claude Agent Skills)에서도 **완전히 공짜는 아니다** — 이름과 짧은
설명은 항상 프롬프트에 실리고, 본문(SKILL.md)만 필요할 때 읽어 온다(점진적 공개).
즉 "설명은 실리고 본문은 안 실린다"이지 "아무것도 안 실린다"가 아니다.
우리 커맨드 툴은 애초에 본문이랄 게 없고 설명 한 줄이 전부라, skill로 바꿔도 줄일 게 없다.

**실제로 줄일 수 있는 것은 툴 설명의 길이와 개수뿐이다.**
- 툴마다 반복하던 공통 문구("추가 인자는 args에 나눠 넣는다", "본인 권한으로 실행된다")를
  **지시문에 한 번만** 두고 툴 설명에서 뺐다. 커맨드 수만큼 곱해지던 비용이 사라진다.
  한 툴 설명이 **약 120자 → 29자**(`내 홈 스토리지 사용량/할당량 조회 [myquota]`)로 줄었다.
- `command_tools_max` 기본값을 200 → **80**으로 낮췄다. 근거: 툴 하나당 (이름+설명+스키마)가
  대략 60~100토큰이고, 컨텍스트 32768에서 지시문·검색결과·대화이력이 이미 20k 안팎을 쓰므로
  커맨드 툴 예산은 ~10k다. 200개면 그 예산을 두 배로 넘긴다.

**정리: 커맨드가 80개를 훨씬 넘어가면 툴 방식은 한계가 온다.** 그때는 (a) 자주 쓰는 것만
카탈로그에 남기거나, (b) 컨텍스트 상한 설정들을 함께 낮추거나, (c) 검색 방식으로 되돌아가는
선택지가 있다. 지금 규모에서는 툴이 낫다(검색이 어긋나 아예 못 찾는 문제가 사라지므로).

## 107. 관리자 콘솔 정리 (완료)

**(1) 부연 설명 제거.** 각 탭 상단과 카드 안의 긴 안내문을 걷어냈다. 남긴 것은 조작에 직접
필요한 한 줄뿐이다(예: 커맨드 탭 "설명을 보고 고르므로 사용자가 물어볼 말로 적으세요.
수정 후 command-mcp 재시작 필요").

**(2) 매뉴얼 업로드 열 선택 단순화.** 제목(section_title)·페이지/순번(page_no) 열 선택을
화면과 API에서 뺐다. 실제로 쓰이지 않는데 업로드할 때마다 고르게 해서 번거로웠다.
남은 것은 **내용(복수 선택)** 과 **문서명** 둘뿐이다.
문서명(`ppt_title`)은 뺄 수 없었다 — #76에서 넣은 "문서 위치 > 가이드 문서 이름" 안내가
이 값으로 만들어진다. 대신 자동 선택되므로 실제로 고를 일은 거의 없다.

**(3) 설정 탭 재구성.** 그룹을 목적별로 다시 짰고, **화이트리스트에 없는 키는 아예 표시하지
않는다**(내부 튜닝값까지 51개를 다 늘어놓으니 정작 바꿔야 할 값을 찾을 수 없었다).
- 순서: **에이전트**(지시문·temperature·이력 상한 + `agent-server 재시작` 버튼) → LLM →
  임베딩·리랭커 → **Manual/Command/VOC/System MCP**(각 그룹에 해당 MCP 재시작 버튼) → Open WebUI
- 그룹마다 재시작 버튼을 두어 "이 값을 바꿨으면 무엇을 재시작해야 하나"가 한눈에 보인다.

**(4) Open WebUI 기본 모델 자동 지정.** `openwebui_admin_api_key`(또는 base_url)를 **저장하면
그 자리에서** 기본 모델 지정까지 이어서 한다. 예전에는 저장 후 별도 버튼을 눌러야 했는데,
agent나 DB를 재기동할 때마다 반복해야 해서 번거롭다는 요청이었다.
동기화가 실패해도 저장 자체는 성공으로 두고 사유만 알린다(키를 막 넣은 직후라 Open WebUI가
아직 준비 전일 수 있다).

## 108. 프롬프트 길이 실측 + 툴 스키마 경량화 (완료)

"지금 프롬프트 길이가 어떠냐, 툴 경량화가 필요하냐"는 질문에 대한 답. **추측하지 않고 쟀다.**
각 MCP 모듈을 실제로 로드해 `mcp.list_tools()`를 호출하고, vLLM에 보내는 OpenAI 함수 정의
모양(`{"type":"function","function":{name,description,parameters}}`) 그대로 직렬화해 글자를 셌다.

### 잰 결과 (경량화 전)

| 툴 | 스키마 글자 |
|---|---|
| `search_voc` | 1,192 |
| `search_manual` | 1,185 |
| `run_command` | 1,059 |
| `get_document` | 1,028 |
| System MCP 7개 | 369~634 (합 3,127) |
| **합계 11개** | **7,577자** |

내장 툴 4개(4,464자)가 전체의 **59%** 였다. 전부 내가 쓴 긴 docstring이고, 내용의 대부분은
지시문(AGENT_INSTRUCTION)에 **이미 같은 말이 들어 있었다** — VOC의 handled_by 처리 방침,
reference를 그대로 옮겨 적으라는 규칙, PII 자리표시자 규칙, "카탈로그 툴을 먼저 쓰라"는 규칙이
툴 설명과 지시문에 이중으로 실려 있었다. 지시문은 **한 번** 실리고 툴 설명은 **툴 수만큼**
실리므로, 중복은 툴 설명 쪽에서 빼는 게 맞다.

`search_manual` 설명에는 `command.search_commands`로 가라는 안내까지 남아 있었다(#106에서
지운 툴이다). LLM에게 없는 툴을 가리키고 있었다.

### 한 것

- 내장 툴 11개의 설명에서 지시문과 겹치는 문장을 전부 뺐다. **7,577자 → 5,272자 (−30%).**
  판단 규칙("이럴 때 쓰고 저럴 땐 쓰지 마라")은 지시문에, 툴 설명에는 **무엇을 하고 무엇을
  돌려주는지만** 남겼다.
- `tests/test_regressions.py`에 **예산 테스트**를 넣었다(6,000자 상한). 설명이 다시 부풀면
  테스트가 먼저 잡는다. 추정 함수(`estimate_prompt_tokens`)도 스키마 고정분을 빠뜨리지
  않는지 검증한다.
- Command MCP 기동 로그에 **실제 비용**을 찍는다:
  `카탈로그 커맨드 N개를 툴로 노출합니다 (툴 M개 · 스키마 X자 ≈ Y토큰/요청)`.
  6,000토큰을 넘으면 경고까지 낸다. "커맨드를 몇 개까지 등록해도 되나"를 감이 아니라
  서버 로그의 숫자로 판단할 수 있다.

### 남은 예산 (32,768 컨텍스트 기준, 토큰은 추정치)

토큰은 한글 1.2자/토큰, 그 외 3.5자/토큰으로 환산했다(폐쇄망에 Qwen 토크나이저가 없어
정밀값 대신 자릿수를 본다).

| 항목 | 크기 | ≈토큰 |
|---|---|---|
| 지시문(AGENT_INSTRUCTION) | 8,109자 | 4,900 |
| 내장 툴 11개 | 5,272자 | 2,700 |
| 카탈로그 툴 (1개당 ~270자) | 20개=5,360자 / 80개=21,440자 | 2,000 / 7,900 |
| 대화 이력(`history_max_chars`) | 8,000자 | 5,400 |
| 매뉴얼 결과 5×1,500자 | 7,500자 | 5,000 |
| VOC 결과 5×1,500자 | 7,500자 | 5,000 |

- **고정비**(매 요청 무조건) = 지시문 + 툴. 카탈로그 20개면 ~9.6k, 80개면 ~15.5k토큰이다.
- 최악의 경우(카탈로그 80개 + 매뉴얼·VOC 둘 다 검색 + 이력 가득) ≈ **30.8k**로 32,768에
  거의 붙는다. 카탈로그 20개면 ~24.9k로 여유가 있다.

**결론: 지금 규모(커맨드 수십 개)에서는 문제없다.** 다만 카탈로그가 커질수록 검색 결과가
밀려나므로, (a) 커맨드 설명은 **한 줄**로 쓰고(콘솔 안내문에 명시), (b) 기동 로그의 토큰 수를
보고 판단하고, (c) 그래도 넘치면 `command_tools_max`를 낮추거나 자주 쓰는 커맨드만 남긴다.

**지시문 8,109자(4.9k토큰)가 단일 항목으로는 가장 크다.** 다만 이건 #62(조건 삭제),
#76(문서 경로 지어내기), #97(실행 결과 조작) 같은 실제 사고를 막으려고 한 줄씩 쌓인 규칙이라,
길다는 이유만으로 자르면 그 문제들이 되살아난다. 지금은 건드리지 않는다.

## 109. 카탈로그 커맨드의 `args`로 빠져나가는 두 경로 (완료)

"`phd info -u {user_id}`는 args 없이 도는 거지? 에이전트가 args에 이상한 걸 넣으면?"에서 출발.
직접 돌려 확인했다.

**args 없이 그대로 돈다.** `args=None`/`[]` 둘 다 `['phd','info','-u','yr9.choi']`가 되고
원격에서는 `su - yr9.choi -c 'phd info -u yr9.choi'`로 실행된다.

**셸 주입은 막혀 있다.** `["; rm -rf /"]`, `` ["`whoami`"] ``, `["$(id)"]`는 전부 통과하지만
`ssh_exec._remote_command`가 `shlex.quote`로 감싸므로 `phd`에 **문자열 인자 하나**로 전달될 뿐
치환·분리가 일어나지 않는다. `["|"]`, 33개 초과, 512자 초과, 제어문자는 애초에 거부된다.

### 그런데 두 군데가 뚫려 있었다

**(1) deny 목록이 `argv[0]`만 봤다.** `phd`에게 `rm`은 무의미한 인자라 무해하지만,
카탈로그에 **인자를 실행하는 커맨드**가 등록되면 이야기가 다르다 — `srun rm -rf ~`,
`xargs chmod 777 /`, `watch reboot`. HPC라 `srun`/`sbatch` 등록은 정상 사용이므로 커맨드
자체를 deny에 넣을 수도 없다.
→ **인자 쪽도 검사한다.** 단, 경로(`/data/kill`)나 옵션(`--rm`)까지 막으면 과잉 차단이므로
`/`가 없고 `-`로 시작하지 않는 '맨 이름' 토큰만 본다.

**(2) `{user_id}` 고정이 뒤 인자로 덮였다.** `phd info -u yr9.choi -u someone_else` —
대부분의 CLI가 **뒤에 온 값**을 쓴다. CLAUDE.md §3의 "user_id는 호출자 헤더에서 강제 주입한다
(남의 자원 접근 불가)"가 이 경로에서만 깨진다. OS 권한은 `su - yr9.choi`로 강등된 채라
파일은 못 보지만, 스케줄러 조회 커맨드가 **자기 권한으로** 남의 job 정보를 뿌리면 새어 나간다.
→ `exec_command`에 `{user_id}`가 있으면 **base_argv에 이미 있는 옵션의 재지정을 거부한다**
(`-u`, `--user=` 형태 모두). `-a` 같은 새 옵션은 그대로 통과한다.

회귀 테스트 2개 추가(`test_catalog_args_cannot_bypass_deny_or_impersonate`,
`test_remote_command_quotes_injection_attempts`). 정상 사용(`srun -n 4 ./my_job.sh`,
`du -h /data/kill`, `ls --rm`)이 막히지 않는지도 함께 고정했다.

### 남는 한 가지 — 등록할 때 지켜야 한다

`{user_id}`가 **없는** 커맨드는 (2)의 보호를 받지 못한다. `myquota -u other`처럼 커맨드가
자체적으로 사용자 지정 옵션을 받으면 막을 방법이 없다 — `-u`의 의미가 커맨드마다 달라
(`ls -u`는 접근시각 정렬) 일괄 차단하면 정상 사용이 깨진다.
**대책: 사용자별 자원을 조회하는 커맨드는 카탈로그에 반드시 `{user_id}`를 넣어 등록한다**
(`phd info -u {user_id}`처럼). 그러면 (2)의 가드가 걸린다.

## 110. Chart MCP 추가 — 추이/비교를 그림으로 (완료)

"사용자가 추이나 그래프를 보고 싶어할 때 호출할 수 있게" 차트 MCP를 추가했다.
참고로 받은 것은 `antvis/mcp-server-chart`.

### antvis 서버를 그대로 쓰지 않은 이유

세 가지가 우리 환경과 안 맞았다.
1. **외부 렌더 서버 의존.** 기본 동작이 데이터를 `antv-studio.alipay.com`으로 보내 이미지를
   받아오는 것이다. 폐쇄망에서는 닿지 않고, 닿더라도 사내 수치를 외부로 보내는 구조라 못 쓴다.
   (`VIS_REQUEST_SERVER`로 사내 GPT-Vis 서버를 지정할 수는 있지만, 그 서버를 따로 띄워야 한다.)
2. **Node 런타임이 새로 필요하다.** 지금은 MCP 4개가 파이썬 이미지 하나를 공유한다.
3. **툴이 25개쯤 된다.** #108에서 잰 대로 툴 하나가 매 요청 ~100토큰이다. 차트 종류마다
   툴을 두면 그것만으로 2.5k토큰이 고정으로 나간다.

그래서 **같은 일을 파이썬 표준 라이브러리만으로** 구현했다. 사내에 GPT-Vis 렌더 서버를 띄우면
그쪽으로 갈아탈 수 있다(그때는 이 MCP의 `render()`만 바꾸면 된다).

### 왜 SVG인가 (matplotlib이 아니라)

- **새 pip 패키지가 없다** → 이미지 재빌드(절차 B)가 필요 없다. 코드만 rsync하고 컨테이너를
  하나 띄우면 끝이다. 폐쇄망에서 이 차이가 크다.
- **한글이 깨지지 않는다.** slim 이미지에는 한글 폰트가 없어서 matplotlib으로 그리면 축 라벨이
  전부 두부(□□□)가 된다. SVG는 글자를 그대로 담고 브라우저 폰트로 그려진다.
- **다크 모드를 같이 담을 수 있다**(`prefers-color-scheme` 미디어쿼리를 SVG 안에).

### 프롬프트 예산을 지킨 두 가지 결정

1. **툴은 하나뿐이다.** `create_chart(chart_type, labels, series, title, y_label)`로
   line/bar/pie/scatter를 모두 처리한다. 스키마 987자(~330토큰) — 차트 종류마다 툴을 두는
   방식의 1/8 수준이다.
2. **이미지 바이트를 돌려주지 않는다.** MCP 툴 결과는 그대로 다음 요청 프롬프트에 실린다.
   base64 PNG를 돌려주면 32768 컨텍스트가 한 번에 날아간다. SVG는 파일로 저장하고
   **URL과 마크다운 한 줄만** 돌려준다(100자 남짓).

### 전달 경로

`create_chart` → SVG 생성 → `/app/charts/<내용해시>.svg` 저장 → MCP와 **같은 포트**의
`/charts/<이름>.svg`로 사용자 브라우저에 직접 서빙 → 에이전트가 `![제목](url)`을 답변에 붙임
→ Open WebUI가 렌더.

- 파일 이름은 **내용의 sha256 앞 32자**다. 같은 차트를 다시 요청해도 파일이 늘지 않고,
  이름을 추측해 남의 차트를 열 수도 없으며, 내용이 바뀌면 이름도 바뀌므로 캐시를 길게 걸 수 있다.
- 정적 서빙은 `^[0-9a-f]{16,64}\.svg$`에 맞는 이름만 연다(경로 조작 차단, 회귀 테스트 있음).
- `chart_public_base_url`을 관리자가 넣어야 한다 — 컨테이너는 자기 외부 주소를 모른다.
  비어 있으면 결과에 `warning`을 실어 에이전트가 사용자에게 알리게 했다(조용한 깨진 링크 방지).
- 오래된 파일은 `chart_retention_hours`(기본 72시간)마다 정리한다.

### 안전

이 MCP는 **아무것도 실행하지 않고 DB도 읽지 않는다.** 호출자가 준 숫자만 그린다.
라벨·제목은 LLM/사용자가 준 문자열이라 전부 XML 이스케이프한다(테스트로 고정).
지시문에는 "그래프를 만들려고 값을 지어내지 말 것"을 넣었다 — #97(실행 결과 조작)과 같은 종류의
사고가 차트에서 재현되지 않게.

## 111. Command MCP + System MCP -> **Execution MCP** 통합 (완료)

사용자 판단: "어차피 실행하는 건 같고, 등록하는 건 전부 whitelist일 거고, agent가 arg를
자유롭게 판단해서 넣을 수도 있어야 한다."

둘로 나눠 뒀던 이유가 실제로는 없었다. 실행 경로는 처음부터 `ssh_exec.run_ssh_as_user` 하나였고,
차이는 **자란 순서**에서 온 것이었다 — 탭이 둘이라 "이건 어디에 등록하지?"가 매번 생겼고,
재시작 규칙도 감사 로그도 두 벌이었다.

### 인자 지정을 어떻게 할 것인가 (설계 판단)

두 방식이 공존하고 있었다.
- Command MCP: `phd info -u {user_id}` 문자열 + LLM에 노출되는 파라미터는 `args` 리스트 **하나**.
  LLM이 무엇을 넣어야 하는지 스키마만 봐서는 알 수 없었다.
- System MCP: `argv_template` JSON 배열 + `params` 타입 정의. 정확하지만 관리자가 JSON을
  손으로 써야 했다.

**둘의 장점만 남겼다.** 관리자는 커맨드 한 줄만 적는다.

```
실행 커맨드:  head -n {lines} {path}
→ 콘솔이 자리표시자를 뽑아 인자 표를 자동 생성
   lines (정수, 기본값 200) · path (문자열, 필수)
```

LLM에게는 `lines: integer`, `path: string (required)`로 **타입이 붙어** 노출된다.
`{user_id}`는 예약어라 표에 나오지 않고 호출자 신원에서 주입된다. 자리표시자 문법은 이미
카탈로그에서 쓰던 것이라 관리자에게 새로 배울 것이 없다.
여기에 **추가 인자 허용** 체크박스를 두어, 정의한 인자 외에 에이전트가 자유롭게 덧붙이는 것도
켜고 끌 수 있게 했다(사용자 요구사항 중 "agent가 arg를 자유롭게 판단").

등록/수정 시 자리표시자와 인자 정의가 어긋나면 **등록 단계에서** 거부한다 — 런타임에 조용히
깨지는 대신 콘솔에서 바로 알려준다.

### 차단 목록(blacklist) — 사용자가 지적한 진짜 위험

"mpirun이나 docker run 뒤에 rm -rf /를 붙여버리면 낭패니까."

정확한 지적이었다. #109에서 인자 검사를 넣었지만 좁은 검사였고, 미등록 커맨드 경로는
여전히 argv[0]만 봤다. **검사를 두 단계로 나눴다.**

| 경로 | 검사 | 이유 |
|---|---|---|
| 등록 커맨드의 추가 인자 | 좁게(맨 이름 토큰만) | 관리자가 승인한 뼈대다. 경로 `/data/kill`이나 옵션 `--rm`을 막으면 정상 사용이 깨진다 |
| 미등록 커맨드(run_command) | **엄격하게**(모든 토큰을 낱말로 쪼개고 경로의 맨 이름까지) | 아무도 승인하지 않았다. 오탐보다 미탐이 위험하다 |

차단 목록에 **셸과 실행 위임 커맨드**를 넣은 것이 핵심이다. `bash`, `sh`, `ssh`, `docker`,
`kubectl`, `chroot`… 이것들이 열려 있으면 나머지 차단이 전부 무의미해진다
(`bash -c "rm -rf /"` 한 줄이면 끝난다).

실측 결과:

| 시도 | 결과 |
|---|---|
| `mpirun -n 4 rm -rf /` | 거부 (`rm`) |
| `docker run --rm -v /:/host alpine rm -rf /host` | 거부 (`docker`) |
| `bash -c "rm -rf /"` | 거부 (`bash`) — 토큰 하나 안에 숨어 있어도 낱말로 쪼개서 잡는다 |
| `srun -n 4 /bin/rm -rf ~` | 거부 (`rm`) — 경로로 우회해도 맨 이름을 본다 |
| `env X=1 rm -rf /` | 거부 (`rm`) |
| `mpirun -n 4 ./my_sim` | **통과** |
| `sinfo` / `squeue -u me` / `awk '{print $1}' x.log` / `cat /etc/hosts` | **통과** |

엄격 검사에는 알고 받아들인 오탐이 하나 있다: 경로에 우연히 `kill` 같은 이름이 든 경우
(`/data/kill`)도 미등록 경로에서는 걸린다. 그때는 커맨드를 정식으로 등록해서 쓰면 된다.

### 이관

물리 DB는 `command_db`를 그대로 쓰고 테이블만 새로 만들었다(`execution_commands`,
`execution_builtin_state`). 이미 올라간 카탈로그와 job_logs를 옮기지 않기 위해서다.
설정 키만 `execution_db_dsn`으로 새로 두고 같은 DB를 가리킨다.

이관은 **파이썬 단계**로 했다(`migrations.import_execution_registry`). SQL로 못 하는 이유가 둘:
`system_custom_commands`가 **다른 데이터베이스**에 있고, 한글 이름에서 ASCII 툴 이름을 만드는
규칙이 파이썬 코드에 있다. 여러 번 돌려도 안전하다(이미 있는 이름은 건너뛴다).
관리자가 바꿔 둔 설정값(`command_tools_max` 등)도 새 키로 값을 옮긴 뒤 옛 키를 지운다 —
통합했다고 설정을 다시 하게 만들지 않는다.

### 곁다리로 정리한 것

`command_catalog`의 임베딩 컬럼을 지웠다. #105에서 RAG를 걷어낸 뒤로 **아무도 읽지 않는데**
업로드할 때마다 수천 건을 임베딩하느라 몇 분씩 걸리고 있었다(콘솔에 "재임베딩" 버튼까지
있었다). 통합하며 컬럼·API·버튼을 함께 제거했다.

### 남은 것

구 `system_db`는 지우지 않고 남겼다(되돌릴 수 있게). 예전 실행 로그도 거기 남아 있어서,
콘솔 실행 로그 화면에는 통합 이후 기록만 보인다.

## 112. #111 통합 배포에서 터진 것 3개 (완료)

전부 내 실수다. 실서버 로그로 확인했다.

### (1) 이관이 조용히 건너뛰어졌다 — 데이터가 하나도 안 옮겨졌다

```
[migrate] execution 레지스트리 모듈을 찾지 못해 이관을 건너뜁니다: No module named 'registry'
```

**원인**: `import_execution_registry`가 한글 이름 -> ASCII 툴 이름 규칙(`tool_name_for`)을
`mcp_servers/execution_mcp/registry.py`에서 import했는데, **db-init 컨테이너에는
`./shared`만 마운트된다**(`docker-compose.dev.yml`). mcp_servers가 없으니 ImportError.

더 나쁜 건 내가 그 ImportError를 `except ImportError: print(...); return`으로 **삼켰다**는 것이다.
db-init은 `[migrate] done`을 찍고 성공한 것처럼 끝났고, 카탈로그도 System 커스텀 커맨드도
새 테이블로 옮겨지지 않았다. 실패를 삼키면 조용히 데이터가 사라진다.

**조치**: `tool_name_for`/`_stable_hash`를 `shared/execution_exec.py`로 옮겼다. 이 규칙은
Execution MCP·관리자 콘솔·db-init **셋이 모두** 써야 하는 것이라 애초에 shared가 맞는 자리였다.
try/except도 걷어냈다(이제 못 찾으면 그냥 터진다 — 조용한 실패보다 낫다).

`linux_exec.py`도 같은 이유로 `shared/`로 옮겼다.

### (2) 포트 충돌로 execution-mcp가 못 떴다

```
Error response from daemon: Bind for 0.0.0.0:8504 failed: port is already allocated
```

**원인**: 내가 준 절차가 `up -d` **다음에** 예전 컨테이너를 지우게 되어 있었다. 순서가 거꾸로다 —
`command-mcp`가 8504를 계속 쥐고 있으니 새 컨테이너가 바인딩에 실패한다.
compose도 `Found orphan containers`로 경고하고 있었다.

**조치**: NEXT-STEPS를 `up -d --no-build --remove-orphans` 한 줄로 고쳤다(별도 삭제 단계 불필요).

### (3) 운영 이미지가 없어진 파일을 복사하고 있었다

`admin_console/Dockerfile`이 `COPY mcp_servers/system_mcp/whitelist.py`를 하고 있었다.
#111에서 그 파일을 `execution_mcp/builtin.py`로 옮겼으므로 운영 이미지 빌드가 실패한다.
(dev는 볼륨 마운트라 안 걸렸다 — 그래서 못 봤다.)

**조치**: `COPY mcp_servers/execution_mcp/builtin.py`로 고쳤다. 겸사겸사 발견한 건데
**예전에도 깨져 있었다** — `whitelist.py` 한 개만 복사했는데 그 파일이 `linux_exec`를
import했고 linux_exec는 이미지에 없었다. (1)에서 linux_exec를 shared로 옮기면서 함께 해결됐다.

### 검증 방법을 바꿨다

이번엔 **실제 Postgres를 띄워** 통합 전 상태(구 카탈로그 4건 + 커스텀 커맨드 2건 +
화이트리스트 상태 2건)를 재현하고 이관을 돌렸다. 6건 이관 + 내장 상태 2건 복사 확인,
세 번 연속 실행해도 6건 유지(멱등) 확인.

회귀 테스트 2개를 추가했는데, **둘 다 고치기 전 코드에서 실패하는지 확인했다**:
- `test_migration_imports_only_shared` — `PYTHONPATH=shared`, `cwd=/`로 격리해
  이관에 필요한 모듈이 전부 shared에 있는지 본다. (1)을 되돌리면 실패한다.
- `test_admin_console_builtin_deps_are_in_shared` — Dockerfile이 없어진 경로를 복사하는지 본다.

### 이관 중에 발견한 것 — 실행할 수 없는 행

구 카탈로그에는 `exec_command`가 비어 있는 행이 있다. 예전에는 그럴 때 **이름을 그대로**
실행했는데, 이름이 한글이면 실행될 수 없는 커맨드다. 툴로 노출하면 프롬프트 예산만 먹고
매번 실패한다. 옮기기는 하되 **비활성**으로 넣고, 몇 건인지 로그에 찍는다 —
관리자가 콘솔에서 실행 커맨드를 채우고 켜면 된다.

## 113. 관리자 콘솔(8501)이 안 뜬 이유 + 이관 로그가 침묵한 이유 (완료)

### 관리자 콘솔이 죽어 있었다 — 이미지에 굳은 CMD

`docker compose ps`에 **admin-console이 없었다**(죽은 컨테이너는 목록에 안 나온다).
Open WebUI(8502)는 되는데 8501만 안 되는 이유였다.

원인은 `dev/Dockerfile.admin-dev`의 CMD다.

```
CMD [... "--reload", "--reload-dir", "/app/admin_console", "--reload-dir", "/app/shared",
     "--reload-dir", "/app/mcp_servers/system_mcp"]
```

#111에서 `mcp_servers/system_mcp`를 없앴고, 서버에서는 rsync `--delete`가 그 디렉토리를
지웠다. **uvicorn은 없는 `--reload-dir`를 주면 기동을 거부한다**:
`Error: Invalid value for '--reload-dir': Path '...' does not exist.` → 컨테이너 즉시 종료.

이 종류가 특히 나쁘다: **CMD는 이미지에 굳어 있어서 코드를 고치고 rsync해도 낫지 않는다.**
그래서 `docker-compose.dev.yml`의 admin-console에 `command:`를 명시해 이미지의 CMD를
덮어썼다 — **이미지 재빌드 없이** 고쳐진다. Dockerfile의 CMD도 함께 고쳤고(다음 빌드용),
경로가 바뀔 수 있는 mcp_servers는 이제 Dockerfile에 넣지 않는다.

### 이관 로그가 아무 말도 안 했다

```
$ docker compose run --rm db-init 2>&1 | grep -E "이관|건너뜁"
(빈 출력)
```

`moved == 0`일 때 아무것도 찍지 않게 만들어 놨었다. 그러면 **"이관이 된 건지 안 된 건지"를
구분할 수 없다** — #112를 겪은 직후인데 또 같은 방식으로 판단을 막았다.

**항상** 출처와 결과를 찍게 했다.

```
[migrate] execution 이관: 카탈로그 2건 · 구 커스텀 커맨드 0건 · 이미 옮겨져 있던 것 2건
          → 신규 0건, 현재 등록 커맨드 총 2건
```

세 상황(옮길 것 있음 / 이미 다 옮겨짐 / 원본이 빔)을 실제 Postgres로 각각 확인했다.
원본이 완전히 비어 있으면 "콘솔에서 직접 등록하세요"까지 안내한다.

### 테스트가 가짜 초록이었다 — 유령 디렉토리

`--reload-dir` 경로 검사 테스트를 처음엔 `os.path.exists`로 썼는데 **통과했다.**
`git rm`은 무시 파일(`__pycache__`)을 지우지 않아서 작업 트리에 `mcp_servers/system_mcp/`가
빈 껍데기로 남아 있었다. 서버는 rsync `--delete`로 그걸 지우므로 로컬만 통과하는 초록이었다.

→ `git ls-files`로 **git이 아는 경로**를 기준으로 판단하게 고쳤다. 고치기 전 CMD로 되돌려
실패하는 것까지 확인했다(처음엔 그 확인에서도 통과해서 테스트가 잘못됐음을 알았다).
로컬의 유령 디렉토리도 지웠다.

**교훈**: 배포 환경과 로컬의 차이(rsync --delete, 이미지에 굳은 CMD, 컨테이너별 마운트)는
`os.path.exists` 같은 로컬 사실로 검증할 수 없다. 저장소가 진실의 원천이어야 한다.

## 114. 차트를 답변에 직접 박는다 — `chart_public_base_url` 없앰 (완료)

"chart_public_base_url 꼭 필요한거야? 사외망으로 연결할 수가 없어. 내부 폐쇄망에서 모두
동작하는 구조여야해."

**먼저 사실 정리**: 그 설정은 사외망과 무관했다. `http://202.20.183.30:8509`처럼 **배포 호스트의
사내 주소**이고, 사용자 브라우저가 이미 쓰는 8502(Open WebUI)와 같은 호스트다. 즉 #110 구조도
폐쇄망 안에서만 돌았다. 다만 지적 자체는 맞는 방향이었다 — 그 설정은 **필요 없게 만들 수 있고,
없는 게 낫다.** 컨테이너는 자기 외부 주소를 모르므로 관리자가 손으로 넣어야 하고, 틀리면
조용히 깨진 이미지가 된다. 이미지용 포트도 하나 더 열어야 한다.

### 바꾼 구조

Chart MCP는 이제 **짧은 표시자**만 돌려준다.

```
{"chart_id": "793d2e...", "markdown": "![월별 GPU 사용률](chart://793d2e...)"}   ← 154자
```

Agent Server가 답변을 **내보낼 때** 그 자리를 `data:image/svg+xml;base64,...`로 바꿔 넣는다
(`shared/chart_inline.py`). SVG는 도커 내부망(`http://chart-mcp:8005/charts/<id>.svg`)으로
가져오므로 브라우저는 Open WebUI 하나만 알면 된다. **설정도, 열어 둘 포트도 없다.**

### 왜 MCP가 처음부터 data URI를 주지 않나

MCP 툴 결과는 **그대로 다음 요청 프롬프트에 실린다**(#110에서 정한 것). 실측하면:

| | 크기 | 어디에 실리나 |
|---|---|---|
| 표시자 (툴 결과) | 154자 | 프롬프트 O |
| 실제 SVG | 2,545자 | 프롬프트 X |
| base64 (답변에 박히는 것) | 3,424자 | 프롬프트 X (사용자에게만) |

치환은 LLM이 보지 않는 '내보내는 텍스트'에서만 일어난다. **대화 이력에도 표시자가 그대로
남는다** — data URI가 저장되면 다음 요청 프롬프트가 3.4k자씩 부푼다. 그래서 `_bg_persist`는
원문을 저장하고, 응답 본문만 치환한다(테스트로 고정).

### 까다로웠던 것 — 스트리밍

표시자가 델타 경계에 걸쳐 쪼개져 온다(`...chart:` / `//793d...`). 그래서 '아직 표시자로 자랄 수
있는 꼬리'만 붙들고 나머지를 흘리는 `_holdback`을 뒀다. 1자씩 흘린 결과와 통째로 치환한 결과가
같은지 250회(1~6자 랜덤 분할 + 1자씩) 확인했다. 붙드는 양은 최대 72자로 유한하다.

같은 차트가 답변에 두 번 나오면 SVG는 한 번만 가져온다. 못 가져오면 깨진 이미지를 조용히
남기지 않고 "차트 이미지를 불러오지 못했습니다"를 적는다.

### 설정은 남겨 뒀다(선택)

`chart_public_base_url`을 넣으면 예전처럼 URL 방식으로 돈다. 답변을 가볍게 하고 브라우저 캐시를
쓰고 싶을 때의 선택지다. **기본값은 비움이고 그게 권장값이다**(콘솔 안내문도 그렇게 고쳤다).

## 115. "내 홈 파일 리스트"가 로그인 서버에서 실행되지 않은 원인 (완료)

원인을 찾았다. **자동 생성되는 상태 행이 실행 위치를 조용히 덮어쓰고 있었다.**

`_is_enabled`는 처음 실행되는 내장 커맨드의 상태 행을 이렇게 만든다.

```sql
INSERT INTO execution_builtin_state (tool_name, enabled) VALUES ($1, $2)
```

`host_mode`를 안 넣었으니 **컬럼 기본값 `'target_server'`가 박힌다.** 그런데 코드는
`list_dir`/`find_files`/`read_file_head`를 `login_server`로 고정해 뒀다(로그인 서버에서
본인 홈을 보는 툴이다). 다음 재시작 때 `_OVERRIDES`가 DB 값을 우선하므로 실행 위치가
`target_server`로 바뀌고, 그러면 **`host`가 LLM 스키마에 노출된다.** 모델이 서버를 골라
넣으면 "내 홈 파일 리스트"가 엉뚱한 서버에서 조회된다.

실제 Postgres로 재현했다.

```
INSERT INTO execution_builtin_state (tool_name, enabled) VALUES ('list_dir', true);
 tool_name | enabled |   host_mode   | updated_by
 list_dir  | t       | target_server |            ← 코드는 login_server인데
```

**구 System MCP에서 그대로 물려온 버그다**(같은 패턴, 같은 컬럼 기본값). 통합 전에도 있었고,
이관이 그 값을 옮겨 오면서 따라왔다.

### 조치

`host_mode`를 **nullable로 바꾸고 기본값을 없앴다. NULL = 코드 기본값을 쓰라는 뜻**이다.
관리자가 콘솔에서 고른 값만 DB에 들어간다.

기존 배포를 고치는 마이그레이션(command_db v9)도 넣었다.
- `updated_by`가 비어 있는 행(= 자동 생성) 의 host_mode를 NULL로.
- 코드가 로그인 서버로 고정한 툴이 `target_server`로 되어 있으면 NULL로. 그건 관리자의
  선택이 아니라 사고다(관리자가 정말 그러길 원하면 콘솔에서 다시 고르면 된다).

회귀 테스트 2개를 넣었다: 컬럼이 nullable이고 DEFAULT가 없는지, 자동 생성 INSERT가
host_mode를 건드리지 않는지, 그리고 로그인 서버 고정 툴의 스키마에 `host`가 없는지.

### 남은 가능성

이 버그가 원인이라면 재시작 후 `list_dir`의 실행 위치가 "로그인 서버 고정"으로 보인다.
그래도 엉뚱한 곳에서 돌면 남은 후보는 두 개다.
  · 에이전트가 `list_dir` 대신 `run_command`를 골랐다(`run_command`는 host가 노출된다).
  · `scheduler_login_host` 설정값이 다른 주소다.
둘 다 답변의 `· 완료 (IP · 계정)` 줄과 실행 로그의 툴 이름으로 바로 가려진다.

## 116. 관리자 콘솔: 내장/등록 커맨드를 한 목록으로 (완료)

"설정탭에서 내장 command, 등록 command 따로 나누지 말고 모두 등록 command 로 해줘"

화면을 하나로 합쳤다. 이름순 한 표에 내장·등록이 섞여 나오고, 활성·실행 위치·설명·역할은
**둘 다 똑같이** 편집한다(행마다 저장 버튼).

내장을 완전히 등록 커맨드로 바꾸지는 않았다. 이유는 값 검증이다 — `read_file_head`의
`lines` 1~2000, `disk_usage`의 `max_depth` 0~10, `system_info`의 `kind` enum,
`safe_path()` 경로 검사는 `exec_command` 템플릿으로 표현할 수 없다. 템플릿으로 옮기면
그 검증이 사라진다. 그래서 **저장 위치는 코드로 남기고 화면만 합쳤다.** 행에 `코드 내장`이
표시되고 커맨드·인자 칸만 회색으로 잠긴다("이건 어느 목록에 있지?"는 없어졌다).

## 117. 비활성 커맨드가 프롬프트 예산을 계속 먹고 있었다 (완료)

"설명을 보고 agent가 tool 고르는거야?"라는 질문을 확인하다 발견했다. 맞다 — 에이전트는
**툴 이름 + 설명**만 보고 고른다. 그래서 설명은 매 요청 프롬프트에 통째로 실린다(#108).

그런데 **비활성으로 끈 커맨드도 툴 목록에 그대로 실리고 있었다.** `enabled`를 실행 시점에만
검사했기 때문이다(끄면 즉시 막히게 하려는 의도였고, 그건 맞았다). 대가가 둘이었다.
  1) 끈 커맨드의 설명이 매 요청 ~100토큰씩 계속 나간다.
  2) 에이전트가 그걸 골라 호출하고 "비활성입니다" 오류를 받는 **헛턴**을 돈다.

**조치**: 툴 목록을 만들 때 비활성을 뺀다(등록 커맨드는 `WHERE enabled`, 내장은 기동 시
상태를 읽어 건너뛴다). 실행 시점 검사는 그대로 두었으므로 **끄는 즉시 막히는 성질은 유지**된다.
비대칭이 생기지만 그게 옳다.

| 동작 | 반영 |
|---|---|
| 끄기 | **즉시**(실행 시점 검사) — 목록에서는 다음 재시작에 사라진다 |
| 켜기 | 재시작 필요(툴 목록을 다시 만들어야 나타난다) |

콘솔도 그렇게 안내하고, 껐다 켤 때만 재시작 배너를 띄운다.

## 118. linux-command-mcp 같은 범용 MCP를 쓸 수 있나 (검토, 채택 안 함)

`github.com/xkiranj/linux-command-mcp` README를 직접 확인했다. 우리 구조와 다른 점이 명확했다.

| | linux-command-mcp | 우리 Execution MCP |
|---|---|---|
| 실행 위치 | MCP 서버가 **자기 머신에서** 실행(Claude Desktop 로컬 프로세스, PM2) | `ssh root@host` → `su - <user_id>` 로 **대상 서버에서 호출자 권한**으로 |
| 실행 주체 | 서버 프로세스의 계정 그대로 | 질문한 사용자 본인 계정 (헤더에서 강제 주입) |
| 셸 | 셸 경유(README가 `sudo` 비밀번호 프롬프트를 언급 = 셸 실행) | 셸 미사용, argv 리스트 |
| 허용 목록 | 없음(`execute-command`에 문자열을 그대로) | 등록 커맨드는 템플릿 고정, 미등록은 차단 목록 전수 검사 |
| 호출자 신원 | 없음 | `X-User-Id` 강제 주입, 역할 검사, 전건 감사로그 |
| 보안 안내 | README: "신뢰된 네트워크에서만", "접근을 제한하라", 민감 정보 조회를 피하라 | 구조로 강제 |

**채택하지 않는 이유는 하나로 요약된다: 실행 주체.** 그 서버는 MCP 프로세스 계정으로 돌린다.
우리 절대 규칙(CLAUDE.md §3)은 "커맨드는 어떤 경우에도 root로 실행하지 않는다. 항상
`su - <user_id>`로 호출자 본인 권한으로 강등한다"다. 컨테이너가 root로 뜨는 우리 환경에서
그걸 붙이면 **모든 사용자가 root로 남의 파일을 읽을 수 있게 된다.** 다중 사용자 시스템에서
쓸 수 없는 구조다(그 README도 단일 사용자 데스크톱을 전제한다).

"리눅스 기본 명령은 LLM이 이미 안다"는 지적은 맞고, 그건 이미 활용하고 있다 — `run_command`가
그 용도다(LLM이 아는 커맨드를 그대로 실행). 내장 7개가 따로 있는 이유는 명령을 **가르치려는
것이 아니라** (a) 인자 값 검증, (b) 실행 위치 고정, (c) 감사 때문이다.

## 119. 참고 문서 경로가 요약돼 나오던 문제 (완료)

Errors: "문서 위치 보여주기는 하는데, 좀 간추려서 보여짐. url 이 안 나옴."

원인은 **내가 편의를 위해 합쳐 준 문자열**이었다. `full_reference()`가 `reference_path`와
`doc_title`을 `" > "`로 이어 `reference` 한 필드로 줬고, 지시문은 "그대로 옮겨 적어라"였다.
그런데 그 문자열이 URL을 포함해 길어서, LLM이 **자기 판단으로 줄였다**
("슈퍼컴 Portal (https://…) > USEFUL INFO. > 활용 가이드" → "슈퍼컴 Portal > 활용 가이드").
"그대로 옮겨라"는 지시만으로는 긴 문자열의 요약을 막지 못한다.

**조치**: 검색 결과에 `guide_location`(위치)과 `guide_document`(문서 이름)를 **따로** 싣고,
지시문에 출력 형식을 코드 블록으로 못박았다.

```
자세한 내용은 다음 문서를 참고하세요:
 - 가이드 위치: <guide_location 그대로>
 - 가이드 문서: <guide_document 나열>
```

"한 글자도 줄이지 않고 그대로 옮긴다. URL이 있으면 URL까지 전부"를 명시했다. 형식이 정해져
있으면 줄일 자리가 없다. `reference`(합친 것)도 호환을 위해 남겼다.

## 120. 실행 결과가 표로 안 나오던 것 + ssh 세션 관측 (완료)

Errors: "job 리스트... 원래 표로 잘 정리해서 보여줬었는데, 이제 너무 느리게 실행되고
간단하게 리스트 형식으로 보여줌."

**표 형식**: 지시문에 "짧고 사실만", "결과 자체만 간결히"만 있었고 표에 대한 말이 없었다.
예전에 표로 나온 건 모델의 그때그때 판단이었다. 규칙으로 만들었다 — 여러 줄·여러 열 출력은
마크다운 테이블로, 열 이름은 출력 헤더 그대로, 값은 바꾸지 않고, 행을 줄이면 줄였다고 밝힌다.
열이 하나거나 몇 줄뿐이면 코드 블록으로 둔다(표가 오히려 번잡해진다).

**느려진 것**: 사용자가 정확히 짚었다 — "채팅이 시작될 때 ssh가 제대로 되는지 확인 필요".
지금까지 `warm_master()`의 성공/실패만 있었고, **마스터 연결이 실제로 살아 있는지 확인하는
수단이 없었다.** 마스터가 죽으면 커맨드마다 TCP+키교환+로그인 셸을 새로 열어 1~3초가 붙는데,
그걸 추측으로 판단할 수 없다.

`ssh -O check`로 확인하는 `master_alive()`를 넣고, 기동 로그에 결과를 남긴다.

```
[execution-mcp] ssh 다중화 마스터 준비 완료(202.20.185.100). 첫 커맨드부터 곧바로 실행됩니다.
[execution-mcp] ssh 마스터를 열지 못했습니다(...). 커맨드는 실행되지만 매번 새로 접속해
                1~3초씩 더 걸립니다. scripts/diag-ssh.sh 로 원인을 확인하세요.
```

유지 루프(keepalive)도 실패를 조용히 넘기지 않고 로그를 남긴다. 이제 "느리다"가 마스터 문제인지
아닌지 로그 한 줄로 갈린다.

## 121. 콘솔 정리 2차 + 설명/역할 (완료)

- **내장 커맨드 설명이 콘솔에서 비어 보였다.** 값으로 `description_override`만 넣고 코드 기본
  설명은 placeholder로 뒀기 때문이다. 이제 **코드 기본 설명을 값으로** 보여준다.
  설명 자체도 짧고 질문 형태로 다시 썼다("내 홈이나 지정한 경로의 파일 목록"). 설명은 에이전트가
  툴을 고르는 유일한 근거이고 매 요청 프롬프트에 실리므로, 80자 상한을 테스트로 걸었다.
- **역할은 select(제한 없음 / user / admin)** 로 바꿨다. 자유 입력이면 오타 하나로 아무도 실행할
  수 없게 된다(역할은 Open WebUI 계정 역할이 그대로 넘어온다).
- **부연 설명 제거**: 괄호 안 설명과 긴 안내문을 걷어냈다(1,200자 이상). 남긴 것은 조작에 직접
  필요한 한 줄뿐이다. 자세한 배경은 이 문서(HISTORY)와 NEXT-STEPS에 있으면 된다 — 콘솔은
  매일 보는 화면이라 설명이 많으면 정작 바꿀 값을 못 찾는다.

---

## 다음 항목은 이어서 여기 아래에 추가
