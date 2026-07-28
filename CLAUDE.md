# AI Infra Assistant — 작업 컨텍스트

세션이 바뀌어도 이어서 작업할 수 있도록, **매 세션 시작 시 아래를 반드시 먼저 읽는다.**

## 0. 세션 시작 시 필독 (순서대로)

1. **`docs/RUN-LOG.md` 전체** — 지금까지의 모든 작업/원인분석 기록(#1부터 최신까지). 일부만
   읽지 말 것. 이전 세션에서 이미 원인을 밝힌 문제를 다시 추측하지 않기 위한 유일한 기억이다.
2. **`Errors`** — 사용자가 회사 서버에서 겪은 최신 에러/요청을 붙여넣는 파일. 이번 턴의 과제.
3. **`docs/NEXT-STEPS.md`** — 사용자가 지금 해야 할 일.
4. `Changes` — 사용자가 서버에서 직접 수정한 내역(사내 pip 미러 대응 등).

## 1. 작업 루프 (사용자와의 약속)

- 사용자가 `Errors`에 에러/요청을 업데이트 → 코드 수정 → 문서 갱신 → **`main`에 바로 커밋·푸시**
  (별도 브랜치·PR 만들지 않는다).
- `docs/RUN-LOG.md`: 원인분석/조치 내역을 번호 이어서 추가. 여기가 기억 저장소다.
- `docs/NEXT-STEPS.md`: **사용자가 할 일만 간결하게.** 설계 설명·부연 금지(그건 RUN-LOG로).
- 답변/문서/커밋 메시지는 한국어.
- 사용자는 폐쇄망 서버에서 직접 명령을 실행한다. 우리가 서버에 접속할 수 없으므로,
  실행할 커맨드를 정확히 주고 결과를 받아서 판단한다.

## 2. 배포 토폴로지

| 대상 | 주소 | 비고 |
|---|---|---|
| 배포 호스트 | 202.20.183.30 | 도커 컨테이너 전부 여기. 코드는 `/home/yrc/AI-Infra-Assistant` |
| 게이트/로그인 서버 | 202.20.185.100 (`login07`) | 내부 서버로 자동 라우팅. ssh 키 등록 완료 |
| GPU 서버 | 75.23.32.41 (`hgpu4041`) | vLLM LLM :8000 / 임베딩 :8010 / 리랭커 :8020 |
| 포트 | agent :8500 · 관리자 콘솔 :8501 · 사용자 웹(Open WebUI) :8502 |

- 모델: LLM `Qwen3-235B-A22B-Instruct-2507-FP8`(TP=4, `--max-model-len 32768`,
  `--enable-auto-tool-choice --tool-call-parser hermes` 필수), 임베딩 `bge-m3`(1024차원 —
  DB 스키마가 `vector(1024)` 고정), 리랭커 `bge-reranker-v2-m3`.
- 폐쇄망: 사내 pip 미러(Nexus)가 패키지를 간헐적으로 `from versions: none`으로 뱉는다 →
  `vendor/*.whl` 오프라인 휠을 쓴다(Dockerfile이 vendor의 모든 휠을 먼저 설치). 이미지는
  인터넷 환경에서 빌드 후 tar로 옮긴다(`scripts/*.sh`).

## 3. 절대 규칙

- **커맨드는 어떤 경우에도 root로 실행하지 않는다.** 모든 실행은
  `shared/ssh_exec.run_ssh_as_user()` 하나만 거치며, 항상 `ssh root@host` → `su - <user_id> -c`로
  호출자 본인 권한으로 강등해 실행한다. 우회 경로를 만들지 않는다.
- 셸을 쓰지 않는다(argv 리스트로 실행). 파괴적 명령은 등록/실행 단계에서 거부한다.
- `user_id`는 LLM 스키마에서 숨기고 호출자 헤더(`X-User-Id`)에서 강제 주입한다(남의 자원 접근 불가).

## 4. MCP 역할 분담 (사용자 결정 사항)

- **Command MCP**: 커맨드 카탈로그 **조회 + 실행**. 화이트리스트 없음 — 카탈로그에 등록된 커맨드는
  전부 실행 가능(`run_command`). 카탈로그 등록 자체가 승인. host 미지정 시 로그인 서버에서 실행.
- **System MCP**: 화이트리스트 관리는 **여기서만**. 항목별 on/off, 필요 역할,
  "분류"(`host_mode`: `login_server` 고정 실행 / `target_server` LLM이 서버 지정),
  콘솔에서 등록하는 커스텀 커맨드(argv_template).
- Manual MCP / VOC MCP: 하이브리드 RAG 검색(읽기 전용). 매뉴얼은 **발행(published)** 해야 검색됨.

## 5. 반영 제약 (자주 놓치는 것)

| 바꾼 것 | 반영 방법 |
|---|---|
| 새 설정 키 / 마이그레이션 | `db-init` 재실행 필수 |
| MCP 툴 추가·설명·`host_mode`·커스텀 커맨드 | 해당 MCP 컨테이너 재시작 |
| `enabled` / `required_roles` | 실시간 반영(재시작 불필요) |
| `agent_system_instruction` | non-force 시드라 **기존 DB에 자동 반영 안 됨** → 콘솔 설정 탭에 직접 붙여넣고 agent-server 재시작 |
| `hot_reload=false` 설정값 | 해당 서비스 재시작(콘솔에 "지금 재시작" 버튼 있음) |
| requirements 변경 | 이미지 재빌드(재시작만으로는 pip 패키지가 안 깔림) |

## 6. 이미 규명된 것 — 다시 추측하지 말 것

- Open WebUI에 모델이 안 보이는 문제: 관리자 패널 → 설정 → **연결(Connections)** 에
  `http://agent-server:8000/v1` 등록 + **모델 공개범위(Visibility)를 Public** 으로.
  (콘솔의 `openwebui_admin_api_key`는 "기본 모델 동기화" 버튼 전용으로, 이것과 무관.)
- 슈퍼 admin은 Open WebUI UI에서 본인 이메일 변경 불가(업스트림 제약). DB 직접 수정 시
  `sqlite3` CLI가 이미지에 없으므로 `python3 -c "import sqlite3..."` 사용.
- MCP 421 Misdirected Request: `FastMCP(..., host="0.0.0.0")` 누락이 원인(해결됨).
- 스트리밍 미동작: ADK `RunConfig(streaming_mode=SSE)` 누락이 원인(해결됨).
