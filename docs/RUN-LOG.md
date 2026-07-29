# 실행 로그 — agent를 서버에 띄우고 운영하는 절차

이 문서는 **서버에 올리고 기동·운영하는 방법**만 담는다.
원인분석·변경 이력은 `docs/HISTORY.md`, 지금 할 일은 `docs/NEXT-STEPS.md`.

실행 위치: **[WSL]** 인터넷 되는 로컬 · **[서버]** 폐쇄망 배포 호스트 202.20.183.30

```bash
# [서버] 모든 작업의 기준 디렉토리
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
```

---

## 1. 코드 반영 (대부분 이 경우 — 재빌드 불필요)

`docker-compose.dev.yml`이 `./mcp_servers`·`./shared`·`./agent_server`를 마운트하므로
코드만 바뀌었으면 재시작만으로 반영된다.

```bash
# [WSL]  GitHub -> 게이트 서버로 전송 (폐쇄망은 GitHub에 못 닿는다)
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
rsync -avz --delete --progress /home/yrc/AI-Infra-Assistant/ \
  yr9.choi@202.20.185.100:/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/

# [서버]
docker compose -f docker-compose.dev.yml run --rm db-init   # 마이그레이션/새 설정 키가 있을 때만
bash scripts/restart-mounted.sh                             # 전체 재시작 + health 확인
```

## 2. 이미지 재빌드 (requirements/Dockerfile이 바뀐 경우만)

```bash
# [WSL] 빌드 -> 저장 -> 전송
bash scripts/rebuild.sh                     # 순차 빌드(병렬 금지 - 사내 미러가 간헐적으로 빈 응답)
TAG=main-$(git rev-parse --short HEAD) bash scripts/save-runtime-images.sh
rsync -avz --progress dist/ai-infra-assistant-runtime-<TAG>.tar \
  yr9.choi@202.20.185.100:/home/gpu1/yr9.choi/05_halo/

# [서버] 로드 -> 재태깅 -> 기동
docker load < ai-infra-assistant-runtime-<TAG>.tar
bash scripts/retag-runtime-images.sh <TAG>
docker compose -f docker-compose.dev.yml up -d --no-build
```
재태깅을 빼먹으면 `No such image: ai-infra-assistant-admin-console:latest`가 난다.

사내 pip 미러가 `from versions: none`을 뱉으면 [WSL]에서 휠을 받아 `vendor/`에 넣는다
(Dockerfile 수정 불필요 — vendor의 모든 휠을 먼저 오프라인 설치한다).
```bash
pip download <pkg>==<ver> -d vendor/
bash scripts/debug-now.sh <pkg>==<ver>      # 미러에 어떤 버전이 있는지 확인
```

## 3. 서비스 재시작

```bash
docker compose -f docker-compose.dev.yml restart agent-server     # 지시문·LLM 설정 변경 후
docker compose -f docker-compose.dev.yml restart command-mcp      # 커맨드 MCP 툴 변경 후
docker compose -f docker-compose.dev.yml restart system-mcp       # 화이트리스트 설명·분류·커스텀 커맨드 변경 후
docker compose -f docker-compose.dev.yml restart admin-console    # 콘솔 코드 변경 후(콘솔 버튼 없음)
bash scripts/restart-mounted.sh                                   # 전부 한 번에
```
콘솔 재시작 버튼으로 되는 서비스: agent-server / manual-mcp / command-mcp / voc-mcp / system-mcp.

## 4. 상태 확인

```bash
docker compose -f docker-compose.dev.yml ps
curl -s http://localhost:8500/health
docker compose -f docker-compose.dev.yml logs agent-server --tail 50
docker compose -f docker-compose.dev.yml logs command-mcp --tail 50
```

웹: 사용자 `http://202.20.183.30:8502` · 관리자 콘솔 `http://202.20.183.30:8501` · agent `:8500`

## 5. GPU 서버(hgpu4041, 75.23.32.41)에서 vLLM 기동

```bash
# LLM (TP=4). --max-model-len과 tool-choice 옵션이 없으면 각각 KV캐시 부족/툴콜 400이 난다.
docker run -dit --rm --gpus all --network host --ipc host \
  -v /home/gpu1/yr9.choi/05_halo/models:/workspace/models \
  --name serve-vllm-llm repo.samsungds.net/docker.io/vllm/vllm-openai:latest \
  --model /workspace/models/Qwen3-235B-A22B-Instruct-2507-FP8 \
  --tensor-parallel-size 4 --gpu-memory-utilization 0.85 --max-model-len 32768 \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --port 8000 --served-model-name qwen3-235b-a22b

# 임베딩 (bge-m3, 1024차원 — DB 스키마 vector(1024)와 일치해야 함)
docker run -dit --rm --gpus '"device=0"' --network host --ipc host \
  -v /home/gpu1/yr9.choi/05_halo/models:/workspace/models \
  --name serve-vllm-embed repo.samsungds.net/docker.io/vllm/vllm-openai:latest \
  --model /workspace/models/bge-m3 --task embed \
  --gpu-memory-utilization 0.08 --port 8010 --served-model-name bge-m3
```

기동 확인:
```bash
curl -s http://75.23.32.41:8000/v1/models
curl -s http://75.23.32.41:8010/v1/models
curl -s http://75.23.32.41:8020/v1/models
```

## 6. 운영 설정값 (관리자 콘솔 → 설정 탭)

| key | 값 |
|---|---|
| `vllm_llm_base_url` | `http://75.23.32.41:8000/v1` |
| `vllm_llm_model` | `qwen3-235b-a22b` |
| `vllm_embed_base_url` | `http://75.23.32.41:8010/v1` |
| `vllm_embed_model` | `bge-m3` |
| `rerank_provider` | `vllm` |
| `rerank_base_url` | `http://75.23.32.41:8020/v1` |
| `rerank_model` | `bge-reranker-v2-m3` |
| `scheduler_login_host` | `202.20.185.100` (이름이 아니라 IP) |
| `show_tool_activity` | `true` (도구 호출/결과를 답변에 접히는 블록으로 표시) |
| `agent_system_instruction` | `docs/NEXT-STEPS.md`의 전문 |

- 콘솔에서 한 번 저장한 값은 `dev-config`가 덮어쓰지 않는다.
- **`docker compose down -v`는 쓰지 말 것** — 설정·매뉴얼·VOC·카탈로그가 전부 사라진다.

## 7. 자주 쓰는 점검

```bash
# 리랭커가 어떤 형식을 받는지 (설정 rerank_provider 결정용)
curl -s -X POST http://75.23.32.41:8020/v1/rerank -H 'Content-Type: application/json' \
  -d '{"model":"bge-reranker-v2-m3","query":"gpu","documents":["gpu 노드","cpu 노드"]}'

# 커맨드가 서버에서 직접 실행되는지 (agent 경로 문제와 구분)
ssh root@202.20.185.100 "su - yr9.choi -c myquota"

# Open WebUI 계정 이메일의 @앞부분이 실제 리눅스 계정과 같은지
ssh root@202.20.185.100 "id yr9.choi"
```

매뉴얼 검색이 이상할 때는 콘솔 → 매뉴얼 탭 → **검색 테스트**로
검색 방식(하이브리드/키워드 전용)·임베딩·리랭커 상태를 먼저 확인한다.
