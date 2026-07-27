# 지금 할 일

## ⚡ 지금 당장 (순서대로)

### -2) hgpu4041 — 임베딩/리랭커 "CUDA busy" — Exclusive_Process 이론은 틀렸음, 로그 필요

`nvidia-smi -c 0`으로 4개 GPU 전부 `Default` 모드로 바꾼 뒤 다시 시도했는데도 임베딩/리랭커가
똑같이 "CUDA busy"로 실패함 → **Exclusive_Process 모드가 원인이 아니었다.** 지금은 다음 둘 중
하나로 추정됨:
- GPU당 실사용 메모리가 이미 72.9~73GiB/81.5GiB(LLM만으로 89%)라 실제로는 **메모리 부족**인데
  vLLM/torch가 에러 메시지를 "busy"로 뭉뚱그려 보여줬을 가능성.
- `docker run`의 `--gpus` 지정 방식이나 컨테이너 안에서 실제로 잡히는 GPU 인덱스가 의도한 것과
  다를 가능성(`--gpus '"device=0"'`이 호스트 인덱스와 컨테이너 안 인덱스가 다르게 매핑되는 경우가
  있음).

**정확한 원인을 알려면 실제 에러 로그가 필요함** — 다음을 Errors에 붙여서 보내줘:
```bash
docker logs serve-vllm-embed --tail 80
docker logs serve-vllm-rerank --tail 80
nvidia-smi   # 임베딩/리랭커 기동 시도 직후 상태(GPU별 메모리 사용량 포함)
```
메모리 부족이 맞다면 LLM의 `--gpu-memory-utilization`을 0.85 → 0.7 정도로 낮추고 재기동해서
GPU당 여유를 더 확보한 뒤 재시도.

### -1) admin-console 빌드 실패 (`openpyxl`, `python-pptx` 못 찾음) — 방금 고침, 코드 갱신 후 재시도

```
ERROR: Could not find a version that satisfies the requirement openpyxl==3.1.5 (from versions: none)
...(openpyxl 고치고 재시도하니) ERROR: ... python-pptx==1.0.2 (from versions: none)
```
사내 미러(Nexus)가 특정 패키지를 간헐적으로 못 찾는 문제(예전 asyncpg와 동일 증상)가
`requirements.txt`를 한 줄씩 순서대로 덮침 — openpyxl 고치니 바로 다음 줄 python-pptx에서
또 발생. `vendor/`에 `openpyxl`/`et_xmlfile`/`python-pptx`/`lxml`/`Pillow`/`XlsxWriter`를
오프라인 휠로 추가했고, `bcrypt`/`docker`(재시작 버튼용) 및 그 의존성도 같은 문제가 나기 전에
미리 추가해뒀다. **혹시 이번에도 requirements.txt의 다음 줄(`redis`)에서 같은 에러가 또 나면**
(이미 vendor에 있어 안 날 가능성이 높지만) 그 패키지 이름/버전을 알려주면 바로 추가한다. WSL에서
최신 커밋을 받고 서버에 반영한 뒤 다시 빌드하면 된다:
```bash
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
rsync -avz --delete --exclude '.env' --progress /home/yrc/AI-Infra-Assistant/ \
  yr9.choi@202.20.185.100:/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/
```
서버(202.20.183.30)에서:
```bash
docker compose -f docker-compose.dev.yml build admin-console
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml ps
curl http://localhost:8500/health
```

### 0) hgpu4041 — 실제 원인 확정: KV 캐시 부족 (busy 에러 아니었음)
```
ValueError: To serve at least one request with the models's max seq len (262144),
(11.75 GiB KV cache is needed, which is larger than the available KV cache memory (8.03 GiB).
```
Qwen3-235B-A22B의 기본 최대 컨텍스트가 262144(256K) 토큰이라, vLLM이 그 길이 하나 처리할
KV 캐시까지 예약하려다 `--gpu-memory-utilization 0.85`로 남는 8GB로는 부족해서 실패한 것
(235B라서, GPU 경합이라서 나는 에러가 아니었음). 이 에이전트는 RAG+툴콜 챗이라 256K 컨텍스트가
필요 없으므로 `--max-model-len`으로 낮추면 KV 캐시 요구량이 그만큼 줄어든다:
```bash
docker run -dit --rm --gpus all --network host --ipc host \
    -v /home/gpu1/yr9.choi/05_halo/models:/workspace/models \
    --name serve-vllm-llm repo.samsungds.net/docker.io/vllm/vllm-openai:latest \
    --model /workspace/models/Qwen3-235B-A22B-Instruct-2507-FP8 \
    --tensor-parallel-size 4 --gpu-memory-utilization 0.85 \
    --max-model-len 32768 \
    --port 8000 --served-model-name qwen3-235b-a22b
```
대화가 길어서 32768(32K)로도 잘리면 65536으로 올려도 되고, 동시 요청을 늘리고 싶으면
`--gpu-memory-utilization`을 0.9 정도로 올려도 된다(단, 임베딩/리랭커가 쓸 여유가 줄어듦).
```
임베딩/리랭커가 OOM 나면(LLM이 GPU당 여유를 너무 적게 남김) LLM의 `--gpu-memory-utilization`을
0.8이나 0.75로 낮춰서 재시도. 이래도 LLM 자체가 busy면 `nvidia-smi --query-gpu=... compute_mode`
결과와 `docker logs serve-vllm-llm`을 같이 보내줘.

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

### 2) hgpu4041 — 임베딩 + 리랭커 기동 (LLM은 확인됨, 이제 이 둘 차례)

**임베딩** (GPU 0, LLM과 같은 GPU를 나눠 씀 — utilization 낮게):
```bash
docker run -dit --rm --gpus '"device=0"' --network host --ipc host \
    -v /home/gpu1/yr9.choi/05_halo/models:/workspace/models \
    --name serve-vllm-embed repo.samsungds.net/docker.io/vllm/vllm-openai:latest \
    --model /workspace/models/Qwen3-Embedding-8B \
    --task embed --gpu-memory-utilization 0.15 \
    --port 8010 --served-model-name qwen3-embedding-8b
```

**리랭커** — 모델이 아직 없으면 먼저 내려받아 전송(인터넷 되는 WSL/서버에서 1회, LLM/임베딩
때와 동일한 패턴):
```bash
huggingface-cli download BAAI/bge-reranker-v2-m3 --local-dir ./models/bge-reranker-v2-m3
rsync -avz --progress ./models/bge-reranker-v2-m3 yr9.choi@75.23.32.41:/home/gpu1/yr9.choi/05_halo/models/
```
확인: `ls /home/gpu1/yr9.choi/05_halo/models/bge-reranker-v2-m3` (hgpu4041에서, config.json 등
있는지). 모델이 준비되면 GPU 1에 `--task score`로 기동(vLLM의 Cohere 호환 `/rerank` 라우트가
자동 노출됨):
```bash
docker run -dit --rm --gpus '"device=1"' --network host --ipc host \
    -v /home/gpu1/yr9.choi/05_halo/models:/workspace/models \
    --name serve-vllm-rerank repo.samsungds.net/docker.io/vllm/vllm-openai:latest \
    --model /workspace/models/bge-reranker-v2-m3 \
    --task score --gpu-memory-utilization 0.15 \
    --port 8020 --served-model-name bge-reranker-v2-m3
```
GPU 1은 LLM(tensor-parallel-size 4라서 GPU 0~3 전부 0.85씩 사용) + 리랭커(0.15)로 딱 채워지는
셈이라 여유가 없다. OOM 나면 0번의 LLM `--gpu-memory-utilization`을 0.8이나 0.75로 낮추거나
리랭커를 0.1로 더 낮춰서 재시도. 상태 확인:
```bash
docker logs serve-vllm-embed --tail 50
docker logs serve-vllm-rerank --tail 50
```

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
계정 관리 기능에 `bcrypt`, "지금 재시작" 버튼 기능에 `docker`(docker-py) 패키지가 새 의존성으로
추가돼서 admin-console은 **재시작만으론 안 되고 이미지를 다시 빌드**해야 함(mounted 코드
재시작으론 pip 패키지가 안 깔림). 또한 `docker-compose.dev.yml`의 admin-console에
`/var/run/docker.sock` 마운트가 새로 추가돼서 **컨테이너 재생성**(`up -d`)까지 해야 소켓이 실제로
붙는다(이미 아래 커맨드에 포함됨). 0번에서 `.env`를 복구했는지 먼저 확인한 다음:
```bash
docker compose -f docker-compose.dev.yml build admin-console
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml ps
curl http://localhost:8500/health
```
⚠️ **보안 트레이드오프**: 이 소켓 마운트로 admin-console 컨테이너는 호스트의 Docker 데몬에
직접 접근한다(사실상 호스트 권한과 다름없음 — 임의 컨테이너 실행/삭제 가능). 재시작 버튼은
허용된 서비스 이름(`agent-server`/`manual-mcp`/`command-mcp`/`voc-mcp`/`system-mcp`)만
재시작하도록 백엔드에서 제한해뒀지만, admin-console 자체가 뚫리면 호스트 전체가 위험해지는
구조이므로 신뢰된 관리자망에서만 이 콘솔을 노출해야 한다.

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
- System MCP 화이트리스트 7개 전부 기본 활성화(`enabled=True`)로 코드 기본값 변경, 설명도 보강.
- 계정 관리 탭 신설(admin 계정 여러 개 관리, `.env` 기본 계정은 잠금 방지용으로 항상 유효).
- 설정 탭 "MCP 엔드포인트" 그룹에 왜 내부망 이름을 그대로 둬야 하는지 경고 카드 추가.
- 설정 탭: `manual/command/voc/system_mcp_url`은 호스트 이름 고정하고 **포트만** 수정 가능하게
  변경(`service_hub_mcp_url`은 실제 외부 주소라 그대로 자유 입력).
- VOC 이력 엑셀 업로드 재작성: 4행부터 헤더, 의뢰내용/조치일/처리내용/만족도만 사용, 조치일·
  처리내용 있는 행만, 만족도 "불만족"·"매우불만족" 제외, 본문 HTML은 태그만 제거하고 내용
  (명령어/코드 포함)은 그대로 보존. 동일 구조 샘플 데이터로 직접 만들어 필터링/정제 결과 검증함.
- 매뉴얼/VOC/커맨드 카탈로그 업로드에 "서버 파일에서 선택" 버튼 추가 — 브라우저 로컬 파일 대신
  서버(202.20.183.30)에 마운트된 폴더(`/home/gpu1/yr9.choi/05_halo/datasets`, 기본값)에서 골라
  올릴 수 있다. 설정 탭 `upload_source_dir`로 그 마운트 하위 폴더를 재시작 없이 바꿀 수 있다
  (마운트 자체를 다른 호스트 경로로 바꾸려면 `.env`의 `UPLOAD_SOURCE_HOST_DIR` + 컨테이너
  재생성 필요 — admin-console 재빌드하면 이 폴더도 같이 마운트됨).
- 설정 탭에서 DB/시크릿(.env로만 관리되는 값) 섹션 자체를 화면에서 제거함(콘솔에서 바꿔도
  효과가 없어서 표시할 이유가 없었음).
- 매뉴얼 탭 버그 수정: 서버 파일(또는 로컬 파일) 선택 시 제목이 비어 있으면 파일명(확장자 제외)
  으로 자동 채워지도록 함 — "제목과 파일을 먼저 선택하세요" 에러가 사실 제목 누락 때문이었을
  가능성이 커서 애초에 안 나게 만듦. 에러 메시지도 제목/파일 중 뭐가 빠졌는지 구분해서 보여줌.
- VOC 엑셀 업로드가 형식을 자동 인식하도록 재작성: 1행 헤더가 Question/Answer(대소문자 무관)면
  이미 정제된 데이터로 보고 그대로 등록(2행부터), 아니면 기존 사내 표준 포맷(4행 헤더)으로 시도.
  둘 다 아니면 두 형식을 모두 안내하는 에러를 띄움.
- vLLM LLM `ValueError`(KV 캐시 부족) 원인 확정 및 해결책 문서화: 위 0번 참고
  (`--max-model-len 32768` 추가).
- 설정 탭 "재시작 필요" 값을 저장한 직후 바로 눌러서 반영할 수 있는 "재시작" 버튼 추가
  (`manual/command/voc/system_mcp_url`, `agent_system_instruction`). System MCP 탭
  화이트리스트에도 활성/비활성 토글·설명 저장·커스텀 커맨드 추가/수정/삭제 중 하나라도 하면
  "⚠ System MCP 재시작" 버튼이 나타나 바로 누를 수 있다. 내부적으로 admin-console 컨테이너에
  마운트한 도커 소켓으로 대상 컨테이너를 `docker restart` 한다 — 위 4번의 보안 트레이드오프
  설명 참고.

## 매뉴얼 탭 — 엑셀 전처리(PPT 등)도 이미 지원

매뉴얼 탭에서 `.xlsx`를 고르면 자동으로 "엑셀 (열 선택)" 모드로 전환된다(코드/커맨드 카탈로그와
동일한 방식). 1행 헤더, 2행부터 데이터인 형식이면 전부 지원 — 예: `ppt_title, slide_index,
section, text, image_files` 같은 PPT 전처리 결과도 그대로 업로드해서 원하는 열들을 체크박스로
골라 내용에 포함시키면 된다(예: section+text+image_files 체크). "제목" 라디오로 섹션 제목 열을,
"페이지/순번" 라디오로 `slide_index`처럼 숫자인 열을 골라 청크에 p번호로 저장할 수 있다(둘 다
선택 사항). 고정된 컬럼 이름을 요구하지 않는다.

## VOC 탭 — "지원하지 않는 형식입니다. 지원: .xlsx" 에러는 헤더 형식과 무관, 확장자 문제

재빌드 후에도 VOC 엑셀 업로드에서 이 에러가 그대로 난다면, 원인은 1행/4행 헤더 자동 인식 로직과
**전혀 관계없다.** 이 메시지는 헤더를 보기도 전에 파일 확장자만 검사하는 코드
(`admin_console/backend/uploads.py`/`server_files.py`)에서 나온다 — 즉 업로드한 파일이 실제로는
`.xlsx`가 아니라는 뜻(예: 옛날 바이너리 `.xls` 형식으로 저장된 파일, 또는 확장자 없이 저장된
파일). Windows 탐색기에서 파일 속성이나 실제 확장자를 확인해보고, 진짜 확장자가 뭔지 알려주면
그에 맞게 지원을 추가한다(`.xls`는 openpyxl이 아예 못 읽는 옛날 포맷이라 별도 라이브러리가
필요해서, 실제로 그 포맷이 맞는지 먼저 확인 후 작업하는 게 맞음).

## 커맨드 카탈로그 — 고정 양식 없음

엑셀 업로드 시 열 이름이 뭐든 상관없다. 업로드하면 실제 파일의 컬럼 목록을 보여주고, 그중 어떤
열을 이름/설명/사용법/카테고리로 쓸지 화면에서 직접 골라 매핑한다(자동 추정도 하지만 그냥 참고용).
고정된 헤더 이름이나 행 위치가 없다.

## 설정 탭 Q&A

- **vLLM 주소 저장 순서**: hgpu4041에서 LLM/임베딩이 실제로 뜬 다음, `vllm_llm_base_url`/
  `vllm_embed_base_url`을 `http://75.23.32.41:8000(또는 8010)/v1`로 바꾸고 저장하면 된다(순서
  그대로 맞음). 저장 즉시(5초 캐시) agent-server가 새 주소로 호출한다 — 재시작/재마운트 불필요.
- **저장 버튼을 누르면 실제로 뭐가 바뀌는지**: `platform_config` DB의 설정 테이블 한 행만
  업데이트된다. 스크립트도 안 돌고 컨테이너도 안 건드린다. "즉시 반영" 값은 각 서비스가 요청마다
  DB를 다시 읽어서(캐시 5초) 자동 반영되고, "재시작 필요" 값(DB DSN, MCP URL)은 그 서비스가
  기동 시 한 번만 읽어서 연결을 맺어두기 때문에 값이 바뀌어도 그 프로세스가 다시 시작해야 반영된다.
- **DB/시크릿을 콘솔에서 못 바꾸게 해놓은 이유**: 콘솔에서 바꾸는 걸 막은 게 아니라, `.env`는
  docker compose가 **컨테이너를 새로 만들 때 딱 한 번**만 읽는 파일이라서 콘솔이 파일 내용을
  고쳐도 그 자체로는 아무것도 안 바뀐다(재현하려면 `docker compose up -d`로 재생성까지 해야 함).
  게다가 DB 비밀번호는 `.env` 값을 바꾸는 것과 별개로 **Postgres 쪽 실제 비밀번호도 같이
  바꿔야** 연결이 안 깨진다 — 둘 중 하나만 바꾸면 그 순간부터 전 서비스 로그인 실패. 이런 이유로
  일부러 여기는 `.env`만 보게 남겨뒀다. 정말 콘솔에서 비밀번호 로테이션까지 하고 싶으면(DB
  ALTER ROLE + .env 갱신 + 관련 서비스 재시작을 한 번에 처리하는 별도 기능) 얘기해주면 설계할 수
  있음 — 지금처럼 "파일만 고쳐준다"는 실제로는 아무 효과가 없어서 그대로 두는 게 맞다고 판단함.

완료/과거 내역은 `docs/RUN-LOG.md` 참고.
