# 지금 할 일

## 지금 당장

### 1) 동기화 버튼 "405: Method Not Allowed" — admin-console이 새 코드를 못 읽은 것

`/api/ops/sync-openwebui-model`은 admin-console 백엔드 코드에 있는데, 405는 그 라우트가 아예
없을 때 정적 파일 서버가 대신 응답하는 에러다(경로 자체는 매칭 안 됐는데 POST라서 405). 즉
admin-console 컨테이너가 아직 최신 코드를 안 읽은 상태 — 코드 최신화 후 admin-console도
재시작해야 한다(이전엔 agent-server/open-webui만 재시작하라고 안내했었음, 빠뜨렸음):
```bash
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
rsync -avz --delete --exclude '.env' --progress /home/yrc/AI-Infra-Assistant/ \
  yr9.choi@202.20.185.100:/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/
```
```bash
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml run --rm db-init
docker compose -f docker-compose.dev.yml restart admin-console agent-server open-webui
```

### 2) 설정 탭 값이 콘솔 재기동하면 mock으로 되돌아가는 문제 — 방금 고침

`dev-config`가 `vllm_llm_base_url` 등을 무조건 mock 값으로 덮어써서, `docker compose down && up`
등으로 서비스가 다시 뜰 때마다 저장한 값이 사라졌던 것. 이제 관리자가 설정 탭에서 한 번이라도
저장한 값(`updated_by`가 bootstrap이 아닌 값)은 안 건드리도록 고침 — 위 1번 커맨드로 최신 코드
반영하면 이후로는 재현 안 됨. 이미 mock으로 되돌아간 상태라면 아래 3번 표대로 다시 한 번만
저장하면 그 뒤로는 유지됨.

### 3) admin_console 설정 탭 — LLM/임베딩/리랭커 값 (되돌아갔으면 다시 저장)

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

### 4) Open WebUI 기본 모델 동기화 (1번 반영 후)

1. `:8502` 접속 → 우측 상단 프로필 → **설정 → 계정 → API 키** 발급(관리자 계정으로).
2. admin_console 설정 탭 → "Open WebUI 연동" 그룹 → `openwebui_admin_api_key`에 붙여넣고 저장.
3. 설정 탭 "LLM" 그룹의 **"Open WebUI 기본 모델 동기화"** 버튼 클릭.

### 5) 확인
```bash
curl http://202.20.183.30:8500/v1/models
```
open-webui(`:8502`)에서 실제 채팅 + 매뉴얼/시스템 조회 등 툴콜 필요한 질문으로 테스트. LLM에
`--enable-auto-tool-choice --tool-call-parser hermes`가 이미 적용돼 있어야 함(아래 6번 참고).

### 6) hgpu4041 — LLM tool-calling 옵션 (아직 안 했으면)
```bash
docker rm -f serve-vllm-llm
docker run -dit --rm --gpus all --network host --ipc host \
    -v /home/gpu1/yr9.choi/05_halo/models:/workspace/models \
    --name serve-vllm-llm repo.samsungds.net/docker.io/vllm/vllm-openai:latest \
    --model /workspace/models/Qwen3-235B-A22B-Instruct-2507-FP8 \
    --tensor-parallel-size 4 --gpu-memory-utilization 0.85 \
    --max-model-len 32768 \
    --enable-auto-tool-choice --tool-call-parser hermes \
    --port 8000 --served-model-name qwen3-235b-a22b
```

완료된 내역/원인 분석은 `docs/RUN-LOG.md` 참고.
