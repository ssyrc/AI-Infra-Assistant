# 지금 할 일

## 지금 당장

### 1) 코드 최신화 (스트리밍 수정 포함 — 전체 컨테이너 재시작 필요)
```bash
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
rsync -avz --delete --exclude '.env' --progress /home/yrc/AI-Infra-Assistant/ \
  yr9.choi@202.20.185.100:/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/
```
```bash
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml restart admin-console agent-server open-webui
```

### 2) 동기화 버튼 405가 계속되면 — admin-console이 실제로 재시작됐는지 직접 확인

1번을 했는데도 "405: Method Not Allowed"가 또 나면, admin-console 컨테이너가 정말 최신 코드로
떴는지 다음으로 확인해줘:
```bash
docker compose -f docker-compose.dev.yml ps admin-console
docker compose -f docker-compose.dev.yml logs admin-console --tail 50
curl -X POST http://localhost:8501/api/ops/sync-openwebui-model
```
(admin-console 컨테이너 안에서 또는 202.20.183.30에서 `localhost` 대신 `202.20.183.30` 사용).
`ps`의 `STATUS`가 방금 재시작한 시각과 맞는지, `logs`에 시작 에러가 없는지, curl 결과가 405가
아니라 다른 응답(400/502 등)인지가 핵심.

### 3) 매뉴얼 RAG가 안 나오는 문제 — 먼저 "발행" 여부 확인

"슈퍼컴 계정 신청 방법"을 못 찾는 건, 등록(업로드)만 하고 **발행(publish)을 안 했을 가능성이
높음** — 검색은 `status='published'`인 문서만 대상으로 한다(초안은 검토 전이라 검색에서 제외).
확인: admin_console → 매뉴얼 탭 → 해당 문서 상태가 `draft`면 클릭해서 열고 **"발행"** 버튼을
누르면 임베딩 후 바로 검색 가능해짐. 이미 `published`인데도 안 나오면 그 문서의 상태/제목
스크린샷이나 목록을 보여줘 — 다른 원인(임베딩 실패 등)을 봐야 함.

### 4) mock으로 테스트할 때 — 실제 mock 컨테이너 이름은 `mock-vllm`

`vllm_llm_base_url`을 `http://mock-llm:8000`으로 바꿔서 테스트했다면, 그 호스트명 자체가
존재하지 않는다(도커 네트워크에 `mock-llm`이라는 컨테이너가 없음 — 실제 mock 서비스 이름은
`mock-vllm`). "AI Infra Assistant"로 계속 뜬 것도 이 때문일 수 있음(agent-server가 그 주소로
연결을 못 해서 이전에 성공했던 실제 백엔드 설정이 남아있었을 가능성). mock으로 테스트하려면:
```
vllm_llm_base_url = http://mock-vllm:8000/v1
vllm_llm_model = mock-llm
```

### 5) 확인
```bash
curl http://202.20.183.30:8500/v1/models
```
open-webui(`:8502`)에서 채팅 응답이 이제 토큰 단위로 스트리밍되는지, 매뉴얼 발행 후 RAG 질문에
답이 나오는지 확인.

완료된 내역/원인 분석은 `docs/RUN-LOG.md` 참고.
