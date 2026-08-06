# Service Hub 연동 규격

사내 통합 VOC 에이전트(Service Hub)와 우리 agent 사이의 연동 계약. **양방향이고 서로 별개**다.

```
 ①  Service Hub ──HTTP POST /v1/voc/query──▶  우리 agent-server (:8500)
     VOC를 우리에게 위임한다. 인증: agent_api_key

 ②  우리 agent-server ──MCP(streamable-http)──▶  Service Hub MCP
     유사 VOC를 조회한다. 설정: service_hub_mcp_url
```

①만으로도 동작한다. ②는 답변에 `similar_voc`를 붙이기 위한 부가 기능이라 주소가 비어 있으면
조용히 생략된다.

---

## ① Service Hub → 우리 agent (VOC 위임)

### 엔드포인트

```
POST http://202.20.183.30:8500/v1/voc/query
Content-Type: application/json
Authorization: Bearer <agent_api_key>
```

**`agent_api_key`가 설정돼 있으면 이 헤더가 필수다.** 없거나 틀리면 401이다.
값은 관리자 콘솔 설정 탭의 `agent_api_key`와 같아야 한다(Open WebUI가 쓰는 것과 같은 키다).

### 요청

```json
{
  "voc_info": {
    "voc_id": "VOC-2026-0001",
    "voc_title": "GPU job이 Queued에서 안 넘어갑니다",
    "voc_class_name": "슈퍼컴/스케줄러",
    "system":     { "id": "SC", "name": "슈퍼컴퓨팅" },
    "sub_system": { "id": "S2", "name": "S2 스케줄러" },
    "division":   { "id": "...", "name": "..." },
    "campus":     { "id": "...", "name": "..." },
    "line":       { "id": "...", "name": "..." },
    "requester":  { "user_id": "yr9.choi", "user_name": "최윤라", "user_dept": "..." },
    "created_at": "2026-08-06T09:12:00+09:00",
    "voc_content": { "text": "제출한 job이 3시간째 Queued입니다.", "raw_text": "<p>...</p>" }
  },
  "output_option": "markdown",
  "stream": false,
  "use_memory": true
}
```

| 필드 | 필수 | 설명 |
|---|---|---|
| `voc_info.voc_content.text` | **예** | 본문. 비어 있으면 `raw_text`의 태그를 벗겨 쓴다. 둘 다 없으면 400 |
| `voc_info.requester.user_id` | 사실상 필수 | **실행 계정이 된다.** 커맨드가 이 계정 권한으로 로그인 서버에서 돈다. 없으면 `anonymous` |
| `voc_info.voc_id` | 권장 | 대화 스레드 키. 같은 VOC의 후속 질문이 이어진다. 없으면 사용자+날짜로 자동 부여 |
| `voc_info.system.name` | 권장 | ②의 유사 VOC 검색을 **같은 시스템으로 좁히는** 필터로 쓴다 |
| `output_option` | 아니오 | `markdown`(기본) 또는 `html`. 답변 형식을 강제한다 |
| `stream` | 아니오 | `true`면 SSE. 기본은 비스트림 JSON(가이드 계약) |
| `use_memory` | 아니오 | 기본 `true`. `requester.user_id` 단위로 장기 메모리를 공유한다 |

나머지 `voc_info` 필드는 전부 선택이며, 있으면 프롬프트의 맥락으로 들어간다.

### 응답

```json
{
  "success": true,
  "answer": {
    "content": "## 확인 결과\n현재 Queued 상태인 job은 2건입니다 ...",
    "similar_voc": [
      { "voc_id": "...", "system": "슈퍼컴퓨팅", "title": "...", "reason": "..." }
    ]
  }
}
```

- 실패 시 `{"success": false, "answer": null, "error": "..."}`.
- `answer.similar_voc`는 ②가 설정돼 있을 때만 붙는다.
- 차트가 생성되면 `content` 안에 data URI 이미지로 치환되어 나간다.

### curl로 확인

```bash
KEY=<agent_api_key>
curl -s -X POST http://202.20.183.30:8500/v1/voc/query \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"voc_info":{"voc_id":"TEST-1","voc_title":"테스트",
       "requester":{"user_id":"yr9.choi"},
       "voc_content":{"text":"내 GPU job 목록 알려줘"}},
       "output_option":"markdown"}' | python3 -m json.tool
```

### 주의 — `requester.user_id`는 실행 신원이다

이 값이 그대로 `X-User-Id`가 되고, 로그인 서버에서 **그 계정으로 커맨드가 실행된다**
(`ssh root@… → runuser -u <user_id>`). 즉 Service Hub가 이 값을 정확히 채워야 하고,
**Service Hub 쪽에서 신뢰할 수 있는 값**이어야 한다. 우리는 `agent_api_key`로 "Service Hub가
맞다"까지만 확인하고, 그 안의 `user_id`는 그대로 믿는다.

## ② 우리 agent → Service Hub MCP (유사 VOC 조회)

`shared/service_hub.py`. 에이전트 툴로 노출하지 않고 `/v1/voc/query`에서 직접 부른다
(결과를 정해진 형태로 매핑해야 해서 LLM에 맡기지 않는다).

| 설정 키 | 값 |
|---|---|
| `service_hub_mcp_url` | Service Hub MCP 주소(streamable-http). **비우면 similar_voc 생략** |
| `voc_similar_top_k` | 붙일 유사 VOC 최대 개수(기본 3, 0이면 비활성) |

호출하는 툴:

| 조건 | 툴 | 인자 |
|---|---|---|
| `system.name` 있음 | `rag_filtered_search` | `query`, `system_name`, `num_result` |
| 없음 | `rag_keyword_search` | `query`, `num_result` |

기대 반환: `{success, data:{total, results:[{title, content, score}]}}`.
결과에 `voc_id`/`system`이 없으므로, 있으면 채우고 없으면 필터로 쓴 `system_name`을 쓴다.
`reason`은 `content` 스니펫으로 만든다. 15초 타임아웃이고, 실패하면 빈 리스트로 넘어간다
(유사 VOC 때문에 본답변이 막히지 않는다).

---

## Service Hub 팀에 전달할 것

1. **엔드포인트**: `POST http://202.20.183.30:8500/v1/voc/query`
2. **인증**: `Authorization: Bearer <키>` — 키는 별도 전달(콘솔 `agent_api_key`와 동일)
3. **요청/응답**: 위 스키마
4. **`requester.user_id`에 사번 계정(예: `yr9.choi`)을 넣어 달라** — 이 값으로 실행된다.
   이메일 형태(`yr9.choi@samsung.com`)로 와도 로컬파트만 쓴다.

우리가 받아야 할 것:

1. **Service Hub MCP 주소** (`service_hub_mcp_url`에 넣는다) — 유사 VOC를 붙이려면 필요
2. 방화벽: 우리 → Service Hub MCP 아웃바운드

## 다른 엔드포인트

VOC 형식이 아닌 일반 질의는 `/v1/agent/query`를 쓴다(같은 인증).

```bash
curl -s -X POST http://202.20.183.30:8500/v1/agent/query \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"user_id":"yr9.choi","message":"내 홈 스토리지 용량 얼마나 써?"}'
```

OpenAI 호환 클라이언트는 `/v1/chat/completions`를 쓰되, 신원을
`X-OpenWebUI-User-Email` 헤더로 보낸다(Open WebUI가 쓰는 경로다).

`/health`만 인증이 없다(신원도 실행도 없고 기동 확인용).
