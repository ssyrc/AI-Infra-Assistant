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

## 19. 임베딩/리랭커 "CUDA busy" 원인 확정 — GPU Exclusive_Process 모드 (문서화, 조치 필요)

LLM(`--max-model-len 32768`)은 정상 기동 확인됨. 이어서 임베딩(GPU 0)·리랭커(GPU 1)를 올리려니
"CUDA busy" 에러. `nvidia-smi` 확인 결과 4개 GPU 전부 `Compute M.: E. Process`
(Exclusive_Process) — 이 모드는 GPU당 CUDA 컨텍스트를 1개로 제한한다(메모리 여유와 무관).
LLM의 tensor-parallel 워커 4개가 이미 GPU 0~3을 하나씩 점유 중이라, 임베딩/리랭커가 같은
GPU에 두 번째 컨텍스트를 열 수 없어서 나는 에러로 확인(예전에 LLM 자체가 안 뜰 때 의심했던
SELinux/Exclusive_Process 이론과는 다른 건 — 그때는 실제로 경로 오타였고, 이번엔 진짜
Exclusive_Process 모드가 원인). 해결책은 `docs/NEXT-STEPS.md` -2번: `nvidia-smi -c 0`으로
Default 모드로 전환 후 LLM/임베딩/리랭커 순서대로 재기동.

## 20. 매뉴얼 엑셀 열 선택 UI 이해 어려움 (완료)

"내용/제목/페이지 체크박스가 뭔지 모르겠다"는 피드백에 번호 매긴 단계별 설명과, 현재 선택으로
첫 번째 행이 실제로 어떻게 저장될지 실시간으로 보여주는 미리보기 박스를 추가함.

---

## 다음 항목은 이어서 여기 아래에 추가
