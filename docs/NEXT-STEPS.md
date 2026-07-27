# 지금 할 일

## ⚡ 지금 당장 (순서대로)

### 0) hgpu4041 — CUDA busy 재발: GPU 1을 리랭커가 독점 중이라 `--gpus all`과 충돌하는 걸로 추정
235B가 커서 나는 에러가 아니다(그랬으면 "busy"가 아니라 OOM 메시지가 난다). `nvidia-smi`의
"Compute M." 칸이 `E. Process`(Exclusive_Process)로 보인다 — 맞다면 GPU 하나에 프로세스가
하나만 붙을 수 있는 모드라, 리랭커가 이미 GPU 1을 물고 있는 상태에서 LLM이 `--gpus all`로
GPU 0~3(1 포함) 전부를 요구하니 충돌한다. 먼저 정확한 모드 확인:
```bash
nvidia-smi --query-gpu=index,compute_mode --format=csv
```
`Exclusive_Process`면 맞은 것. **순서를 바꿔서**: GPU 4장이 다 비어있을 때 LLM부터 먼저 올리고,
임베딩/리랭커는 LLM이 뜬 뒤 각 GPU의 남는 용량(0.85 사용 시 GPU당 ~15% = ~12GB)에 나중에 얹는다.
```bash
docker stop serve-vllm-rerank   # 잠깐 내려서 GPU 4장 다 비우기

docker run -dit --rm --gpus all --network host --ipc host \
    -v /home/gpu1/yr9.choi/05_halo/models:/workspace/models \
    --name serve-vllm-llm repo.samsungds.net/docker.io/vllm/vllm-openai:latest \
    --model /workspace/models/Qwen3-235B-A22B-Instruct-2507-FP8 \
    --tensor-parallel-size 4 --gpu-memory-utilization 0.85 \
    --port 8000 --served-model-name qwen3-235b-a22b

# LLM이 뜬 것 확인 후(healthy 될 때까지 몇 분 걸릴 수 있음)
curl http://localhost:8000/v1/models

docker run -dit --rm --gpus '"device=0"' --network host --ipc host \
    -v /home/gpu1/yr9.choi/05_halo/models:/workspace/models \
    --name serve-vllm-embed repo.samsungds.net/docker.io/vllm/vllm-openai:latest \
    --model /workspace/models/Qwen3-Embedding-8B \
    --task embed --gpu-memory-utilization 0.15 \
    --port 8010 --served-model-name qwen3-embedding-8b

docker run -dit --rm --gpus '"device=1"' --network host --ipc host \
    -v /home/gpu1/yr9.choi/05_halo/models:/workspace/models \
    --name serve-vllm-rerank repo.samsungds.net/docker.io/vllm/vllm-openai:latest \
    --model /workspace/models/bge-reranker-v2-m3 \
    --task score --gpu-memory-utilization 0.1 \
    --port 8020 --served-model-name bge-reranker-v2-m3
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

### 2) hgpu4041 — 리랭커 (완료, GPU 1에 이미 떠 있음)
`BAAI/bge-reranker-v2-m3`를 vLLM `--task score`로 GPU 1에 띄운 게 이미 정상 동작 중(vLLM의
Cohere 호환 `/rerank` 라우트 노출). LLM/임베딩만 0번 재시도하면 됨.

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
계정 관리 기능에 `bcrypt`가 새 의존성으로 추가돼서 admin-console은 **재시작만으론 안 되고
이미지를 다시 빌드**해야 함(mounted 코드 재시작으론 pip 패키지가 안 깔림). 0번에서 `.env`를
복구했는지 먼저 확인한 다음:
```bash
docker compose -f docker-compose.dev.yml build admin-console
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml ps
curl http://localhost:8500/health
```

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
