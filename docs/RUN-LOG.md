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

## 34. 에이전트가 "슈퍼컴" 관련 질문에 호스트를 안 밝히면 되묻기만 함 (커맨드 안내함)

`disk_free(user_id, host)`가 host를 필수로 받는데, LLM이 실제 로그인 서버 이름을 모르니
"어떤 시스템 기준이냐"고 되묻는다. `scheduler_login_host`(Command MCP 전용, disk_free와는
별개 설정)와 별개로, `agent_system_instruction`에 로그인 서버 이름(`login07`)과 "되묻지 말고
바로 호출하라"는 문장을 추가해야 함 — 정확한 문구는 `docs/NEXT-STEPS.md` 5번 참고(설정 탭에서
직접 저장 후 agent-server 재시작 필요, hot_reload=false 키라서).

## 20. 매뉴얼 엑셀 열 선택 UI 이해 어려움 (완료)

"내용/제목/페이지 체크박스가 뭔지 모르겠다"는 피드백에 번호 매긴 단계별 설명과, 현재 선택으로
첫 번째 행이 실제로 어떻게 저장될지 실시간으로 보여주는 미리보기 박스를 추가함.

---

## 다음 항목은 이어서 여기 아래에 추가
