# 지금 할 일

## 지금 당장

### 1) "AI Infra Assistant"가 모델 목록에서 안 보임 — 진단 순서

admin_console 설정 탭의 `vllm_llm_base_url`은 agent-server가 **vLLM에** 접속하는 주소라
Open WebUI 모델 목록과는 무관하다(별개 설정). Open WebUI는 `OPENAI_API_BASE_URL`
(`http://agent-server:8000/v1`, docker-compose 환경변수로 이미 고정)로 agent-server에 붙어서
`/v1/models`를 읽어오는 구조다. 어디서 끊겼는지 순서대로 확인:

```bash
# 1) agent-server 자체가 여전히 정상 응답하는지
curl http://202.20.183.30:8500/v1/models
# {"object":"list","data":[{"id":"AI Infra Assistant",...}]} 나와야 함

# 2) open-webui 쪽 로그에 연결 에러가 있는지
docker compose -f docker-compose.dev.yml logs open-webui --tail 80
```

1번이 실패하면 agent-server 문제(재시작 필요할 수 있음: `docker compose -f docker-compose.dev.yml
restart agent-server`). 1번은 되는데 Open WebUI에 안 보이면 Open WebUI 쪽 UI 확인:

3. `:8502` → 프로필 → **관리자 패널 → 설정 → 연결(Connections)** → OpenAI API 항목에
   `http://agent-server:8000/v1`이 등록/활성화돼 있는지 확인(WEBUI_AUTH를 켜고 영구 볼륨으로
   바꾸면서 DB가 새로 시작돼 이 연결이 비어있을 수 있음 — 없으면 URL 추가하고 키는 아무 값이나
   입력, "not-needed"도 가능).
4. **관리자 패널 → 설정 → 모델(Models)** → "AI Infra Assistant"가 목록엔 있는데 꺼져 있는 건
   아닌지(눈 아이콘/토글로 개별 모델을 숨길 수 있음) 확인.

어느 단계에서 막히는지 알려주면 다음 조치를 정확히 짚어줄 수 있음.

완료된 내역/원인 분석은 `docs/RUN-LOG.md` 참고.
