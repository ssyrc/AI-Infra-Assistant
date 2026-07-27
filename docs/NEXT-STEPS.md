# 지금 할 일 — 실제 vLLM 연결 (hgpu4041 / 75.23.32.41)

mock-vllm 대신 실제 LLM을 붙여서 admin_console / open-webui 기능 테스트.

## 1. GPU 서버(hgpu4041)에서 vLLM 기동

Chat 모델:
```bash
vllm serve <모델 경로> --served-model-name qwen3-32b --port 8000 --host 0.0.0.0
```

임베딩 모델 (메모리/RAG 테스트용):
```bash
vllm serve BAAI/bge-m3 --task embed --served-model-name bge-m3 --port 8010 --host 0.0.0.0
```

> API 키는 걸지 않는다 — agent_server가 `api_key="not-needed"`로 하드코딩되어 있어 인증을 걸면 실패한다.

## 2. 도달 확인 (GPU 서버 기준)

```bash
curl http://75.23.32.41:8000/v1/models
curl http://75.23.32.41:8010/v1/models
```

## 3. admin_console 설정 탭에서 연결

`http://<agent서버>:8080` → 설정 탭 → 아래 값 입력 (저장 즉시 반영, 재시작 불필요):

| 키 | 값 |
|---|---|
| `vllm_llm_base_url` | `http://75.23.32.41:8000/v1` |
| `vllm_llm_model` | `qwen3-32b` |
| `vllm_embed_base_url` | `http://75.23.32.41:8010/v1` |
| `vllm_embed_model` | `bge-m3` |

⚠️ `docker compose -f docker-compose.dev.yml down` 후 다시 `up` 하면 `dev-config`가 이 값을 mock으로 재덮어씀. 컨테이너를 살려둔 채로만 설정 변경.

## 4. 연결 확인

```bash
curl http://<agent서버>:8500/v1/models
```
`qwen3-32b`가 응답에 나오면 정상.

## 5. 기능 테스트

- open-webui (`:3000`) 채팅 → mock 에코가 아닌 실제 응답 확인
- admin_console 메모리/RAG 관련 기능 → 임베딩 연결 확인
