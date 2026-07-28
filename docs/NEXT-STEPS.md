# 지금 할 일

## 지금 당장

### 1) 코드 최신화 (LLM/임베딩/리랭커 모델명 표시 버그 수정 + Arena 모델 제거)
```bash
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
rsync -avz --delete --exclude '.env' --progress /home/yrc/AI-Infra-Assistant/ \
  yr9.choi@202.20.185.100:/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/
```
서버(202.20.183.30)에서 재시작(신규 pip 의존성 없음, 재빌드 불필요 — 코드/compose만 바뀜):
```bash
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml restart agent-server open-webui
```

### 2) admin_console 설정 탭 — LLM/임베딩/리랭커 값 저장 (모델 다 뜬 상태)

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

⚠️ `docker compose -f docker-compose.dev.yml down` 후 `up`하면 `dev-config`가 이 값들을 mock으로
재덮어씀. 컨테이너를 살려둔 채로만 설정 변경.

### 3) 확인
```bash
curl http://202.20.183.30:8500/v1/models   # "AI Infra Assistant"로 나오는지
```
- open-webui(`:8502`) 새로고침 → 모델 목록에 mock-llm/arena model 없이 "AI Infra Assistant"만
  보이는지, 실제 채팅 응답이 오는지 확인.
- admin_console 매뉴얼/VOC 등록 → 발행(임베딩 호출) 정상 동작 확인.

완료된 내역/원인 분석은 `docs/RUN-LOG.md` 참고.
