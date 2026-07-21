# AI-Infra-Assistant 실행 가이드 (폐쇄망)

이 문서는 **처음부터 끝까지** 순서대로 따라 하면 되는 실행 가이드다. 폐쇄망(사내 미러, 프록시)
기준으로 작성했고, dev(mock, LLM 불필요) → 프로덕션 순으로 넘어간다.

- 스모크 테스트/단위 테스트의 세부는 `docs/TESTING.md` 참고.
- 아키텍처/데이터 전처리 설명은 `README.md` 참고.

---

## 0. 한눈에 보기

| 구분 | 값 |
|---|---|
| agent-server 외부 포트 | **8500** (외부 VOC agent 접속 / 방화벽 개통 대상) |
| 관리자 콘솔 | 8080 (admin/admin, dev 기준) |
| 사용자 웹(Open WebUI) | 3000 |
| MCP 서버(호스트 노출) | manual 8501 · command 8502 · voc 8503 · system 8504 |
| PostgreSQL | 5432 |
| dev 기동 | `docker compose -f docker-compose.dev.yml up -d --build` |
| 프로덕션 기동 | `docker compose up -d --build` |

> 호스트 포트가 겹치면 `.env`에서 `AGENT_PORT`/`ADMIN_PORT`/`OPENWEBUI_PORT`/`PG_PORT`/`*_MCP_PORT`만
> 빈 포트로 바꾼다. 사용 중 포트 확인: `ss -tlnp | grep -E ':(3000|5432|8080|8500|8501|8502|8503|8504)\b'`

---

## 1. 사전 준비 (폐쇄망 미러)

### 1-1. 컨테이너 이미지 미러
`.env`에 사내 레지스트리 접두사를 넣는다(비우면 공개 레지스트리):

```bash
REGISTRY_DOCKERHUB=repository.samsungds.net/proxy-docker-registry-1.docker.io
REGISTRY_GHCR=repository.samsungds.net/proxy-docker-ghcr.io
```

dev에 필요한 이미지 4종:
```
<REGISTRY_DOCKERHUB>/pgvector/pgvector:pg16
<REGISTRY_DOCKERHUB>/postgres:16-alpine
<REGISTRY_DOCKERHUB>/python:3.12-slim
<REGISTRY_GHCR>/open-webui/open-webui:v0.6.5
```
프로덕션은 추가로 `langfuse/langfuse:3.130.0`, `langfuse/langfuse-worker:3.130.0`,
`clickhouse/clickhouse-server:24.8`, `redis:7.4-alpine`, MinIO.

### 1-2. pip 미러 (빌드 시 파이썬 패키지)
`.env` 값을 사내 pip.conf와 동일하게:
```bash
PIP_INDEX_URL=http://repository.samsungds.net/repository/proxy-pypi-files.pythonhosted.org/simple
PIP_TRUSTED_HOST=repository.samsungds.net
BUILD_PROXY=http://202.20.187.241:3128
NO_PROXY=localhost,127.0.0.1
```

> ⚠️ **빌드 중 `JSONDecodeError: Expecting value: line 1 column 1`** 이 나면 → 3장 참고.
> 이건 프록시 문제가 아니라 **pip 버전** 문제이고, 리포에 이미 해결책(vendor 휠)이 포함돼 있다.

---

## 2. 코드 받기 / 최신화

### 2-1. 최초
```bash
git clone <repo> AI-Infra-Assistant
cd AI-Infra-Assistant
git checkout main
```

### 2-2. 이미 받아둔 서버를 최신으로 (중요)
> 빌드가 계속 같은 에러로 실패하면 **서버의 코드가 옛날 버전**인 경우가 대부분이다.
> 로컬에서 손으로 고친 Dockerfile 등이 있으면 `git pull`이 막히니, main과 동일하게 강제 정렬한다:

```bash
cd AI-Infra-Assistant
git fetch origin main
git reset --hard origin/main    # 로컬 수동수정 폐기, main과 동일하게 (vendor 휠 포함 딸려옴)
```

정상 반영 확인:
```bash
git log --oneline -1
ls -l vendor/pip-22.2.2-py3-none-any.whl          # 약 2,044,706 bytes 있어야 함
grep -rn 'pip<23' */Dockerfile* 2>/dev/null       # 아무것도 안 나와야 함
grep -n vendor mcp_servers/Dockerfile             # COPY vendor/ /tmp/vendor/ 나와야 함
```

> **서버에서 git이 안 되면**(폐쇄망) `git reset` 대신 **3-A의 직접 패치 스크립트**를 쓴다.
> 그게 6개 Dockerfile을 한 번에 올바른 상태로 맞춘다.

---

## 3. 폐쇄망 pip `JSONDecodeError` — 원인과 해결(이미 반영됨)

**증상**: 이미지 빌드 중
```
json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
```

**원인 (프록시 아님)**: `python:3.12-slim` 내장 **최신 pip(24.x)** 는 인덱스를 **JSON Simple
API(PEP 691)** 로 조회하는데, 사내 미러가 이를 지원하지 않아 응답을 `json.loads()` 하다 깨진다.
사내 호스트의 pip가 되는 이유는 그 pip가 **HTML API만 쓰는 옛 버전(<22.3)** 이기 때문.
`pip install "pip<23"` 로 내리려는 시도도 그 설치 자체가 최신 pip로 실패한다(닭-달걀).

**해결 (리포에 포함, 추가 작업 불필요)**: `vendor/pip-22.2.2-py3-none-any.whl`(HTML API만 쓰는
순수 파이썬 휠)을 각 Dockerfile이 빌드 첫 단계에서 **오프라인(`--no-index`)** 으로 먼저 설치한다.
이후 모든 패키지는 옛 pip로 사내 미러에서 정상 설치된다.

빌드 로그에서 아래처럼 나오면 정상:
```
=> [command-mcp] COPY vendor/ /tmp/vendor/
=> [command-mcp] RUN ... pip install --no-index ... pip-22.2.2-py3-none-any.whl
   → Successfully installed pip-22.2.2
=> [command-mcp] RUN pip install ... -r requirements.txt      ← 미러 정상 동작(JSON 에러 없음)
```

> 휠이 서버에 없거나 GitHub를 못 받는 경우엔 인터넷 되는 곳에서 한 번:
> `pip download pip==22.2.2 --no-deps -d vendor/` 로 만들어 `vendor/`에 두면 된다.

### 3-A. GitHub 동기화가 안 되는 서버용 — 직접 패치 스크립트 (git 불필요)

폐쇄망이라 서버에서 `git pull`/`git reset`이 안 되거나, Dockerfile을 손으로 고쳐 **일부 파일만
vendor 단계가 있고 일부는 없는** 상태라면(예: `mock-vllm`은 되는데 `agent-server`/`admin-console`은
JSONDecodeError), 아래 **한 블록**을 서버에서 그대로 실행한다. 6개 Dockerfile 전부에 vendor
부트스트랩을 넣고 잘못된 흔적(`pip<23`, `Temp/pip`, `22.3.1`)을 제거한다. 재실행해도 안전(멱등).

```bash
cd AI-Infra-Assistant

# 1) vendor 휠을 22.2.2로 보장  (⚠️ 22.3.1은 여전히 JSON API를 써서 안 됨! 반드시 < 22.3)
mkdir -p vendor
rm -f vendor/pip-22.3.1*.whl
[ -f vendor/pip-22.2.2-py3-none-any.whl ] || pip download "pip==22.2.2" --no-deps -d vendor/
ls -l vendor/pip-*.whl

# 2) 6개 Dockerfile 전부에 vendor 부트스트랩 주입 (WORKDIR /app 바로 뒤)
for f in agent_server/Dockerfile admin_console/Dockerfile mcp_servers/Dockerfile \
         shared/Dockerfile.db-init dev/Dockerfile.mock dev/Dockerfile.admin-dev; do
  [ -f "$f" ] || { echo "없음: $f"; continue; }
  sed -i -e '/pip<23/d' -e '/COPY pip.conf/d' -e '/Temp\/pip-/d' -e '/pip-22.3.1/d' "$f"
  if grep -q '/tmp/vendor/pip-' "$f"; then echo "OK(이미 있음): $f"; continue; fi
  awk '
    { print }
    !ins && /^WORKDIR[[:space:]]+\/app/ {
      print "COPY vendor/ /tmp/vendor/"
      print "RUN pip install --no-index --no-cache-dir /tmp/vendor/pip-*.whl"
      ins=1
    }
  ' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  echo "패치: $f"
done

# 3) 확인 (6줄 모두 =1, "잔재 없음 OK" 이어야 함)
echo "=== 확인 ==="
for f in agent_server/Dockerfile admin_console/Dockerfile mcp_servers/Dockerfile \
         shared/Dockerfile.db-init dev/Dockerfile.mock dev/Dockerfile.admin-dev; do
  printf "%-35s =%s\n" "$f" "$(grep -c 'no-index --no-cache-dir /tmp/vendor/pip' "$f")"
done
grep -rn 'pip<23\|Temp/pip\|22.3.1' */Dockerfile* 2>/dev/null && echo "!!! 잔재 남음" || echo "잔재 없음 OK"
```

**기대 출력**: `vendor/pip-22.2.2-py3-none-any.whl` 존재, 6줄 모두 `=1`, `잔재 없음 OK`.
하나라도 `=0`이면 그 파일에 `WORKDIR /app` 줄이 없는 것이니 알려달라.

그다음 빌드:
```bash
docker builder prune -af
docker compose -f docker-compose.dev.yml build --no-cache
docker compose -f docker-compose.dev.yml up -d
```

> **왜 22.3.1이 아니라 22.2.2인가**: pip는 **22.3부터** JSON Simple API(PEP 691)를 쓴다. 그래서
> `pip==22.3.1`을 오프라인으로 깔아도 이후 미러 조회에서 **똑같이 JSONDecodeError**가 난다.
> HTML API만 쓰는 **22.2.2(< 22.3)** 여야 한다.

---

## 4. `.env` 만들기

`.env.example`은 **템플릿(깃에 커밋됨)** 이고, docker compose가 실제로 읽는 건 **`.env`** 다.
`.env`는 `.gitignore` 대상이라 서버에서 직접 만든다.

```bash
cp .env.example .env
```

- **dev(트랙 A)**: `CHANGE_ME`(비밀번호/키)는 **안 건드려도 된다**. dev compose가 DB 비번을
  `devpass`로 고정하고 LLM도 mock으로 덮어쓴다. 중요한 건 1장의 미러/pip/포트 값뿐(이미 채워져 있음).
- **프로덕션(트랙 B)**: `.env`의 모든 `CHANGE_ME`를 실제 값으로 채운다:
  ```bash
  openssl rand -base64 32    # 비밀번호류
  openssl rand -hex 32       # LANGFUSE_ENCRYPTION_KEY
  ```
  그리고 `VLLM_*`(LLM/임베딩 주소), `SSH_KEY_PATH`(원격 실행용) 확인.

---

## 5. 트랙 A — dev/mock 기동 (LLM 불필요, 권장)

```bash
docker compose -f docker-compose.dev.yml build --no-cache
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml ps      # 서비스 Up / db-init·dev-config는 Exit 0
```

동작 확인:
```bash
curl -s http://localhost:8500/health ; echo
curl -s http://localhost:8500/v1/models ; echo
```

- 관리자 콘솔: http://<서버IP>:8080 (admin/admin)
- 사용자 웹: http://<서버IP>:3000

스모크 테스트(외부 agent API, VOC 계약, 메모리 적재 등)는 `docs/TESTING.md` 트랙 A 참고.

정리:
```bash
docker compose -f docker-compose.dev.yml down -v   # -v: DB 볼륨까지 삭제
```

---

## 6. 트랙 B — 프로덕션 (vLLM 준비 후)

```bash
cp .env.example .env      # CHANGE_ME 전부 실제 값으로
docker compose up -d --build
```

기동 후:
1. 관리자 콘솔 → **설정 탭** → `vllm_*`/`rerank_*` 주소가 실제 서버를 가리키는지 확인(즉시 반영).
2. **사용자별 메모리(Open WebUI)**: prod compose에 `ENABLE_FORWARD_USER_INFO_HEADERS=true` 존재 →
   로그인 이메일 → 계정 매핑 확인.
3. **Langfuse**: 키 입력 후 `docker compose up -d agent-server` → Users/Sessions 뷰에서 user_id별 이력 확인.
4. **SSH 실행 툴(System/Command MCP)** — 기본 비활성:
   - `SSH_KEY_PATH`(대상 서버 root 키) + 호스트 `/etc/hosts`(예: `202.20.185.100 login05`) 마운트 확인
   - 설정 `scheduler_login_host`(기본 login05)
   - 관리자 콘솔 **System 탭**에서 툴 토글 ON
   - 테스트: "hgpu8002 GPU 상태" 질의 → `gpu_status` 실행 확인
5. **Service Hub(similar_voc)**: 방화벽 개통 후 설정 탭 `service_hub_mcp_url` 입력 → VOC 요청으로
   `similar_voc`/`voc_id` 확인.

---

## 7. 방화벽

외부(통합 VOC agent 등)에서 우리 agent-server로 들어오는 **인바운드** 기준. 구성에 따라 하나만 선택:

| 구성 | 열 포트 | 비고 |
|---|---|---|
| ① agent-server 직결(가장 단순) | **인바운드 8500/tcp** | VOC agent IP에서만 허용. `http://<서버IP>:8500/v1/voc/query` |
| ② 리버스 프록시 + TLS | **인바운드 443/tcp** (+80 리다이렉트) | 프록시가 내부에서 8500으로 전달. 8500은 외부 개방 불필요 |

> 평문 HTTP가 허용되면 ①(8500 하나)이 제일 단순하다. 둘 다 열 필요는 없다.

**아웃바운드(참고, 인바운드 아님)**:
- Service Hub MCP 연동: agent 호스트 → `innodev--etl-prod.cdep.samsungds.net` **80/443**
- 원격 실행(System/Command MCP): agent 호스트(202.20.183.30) → 대상 서버 **22(ssh)**

---

## 8. 자주 겪는 문제 (Troubleshooting)

### 8-1. 빌드 중 `JSONDecodeError` 가 계속 난다
→ 3장. 십중팔구 **서버 코드가 옛날 버전**이거나 **일부 Dockerfile만 고쳐진 상태**다.
- git이 되면: `git reset --hard origin/main`
- git이 안 되면: **3-A 직접 패치 스크립트** 실행
그 후 `grep -c 'no-index --no-cache-dir /tmp/vendor/pip'` 가 **6개 파일 모두 `1`** 인지,
`grep -rn 'pip<23\|Temp/pip\|22.3.1' */Dockerfile*` 가 **아무것도 안 나오는지** 먼저 확인하고 빌드한다.
(로그가 매번 100% 동일하면 = 파일이 안 바뀐 것. `22.3.1`은 여전히 JSON을 쓰므로 반드시 `22.2.2`.)

### 8-2. 포트 충돌 (`address already in use`, 8000/8100 등)
→ agent-server는 기본 **8500**으로 노출된다. 그래도 겹치면 `.env`에서 해당 `*_PORT`만 빈 포트로 바꾼다.
```bash
ss -tlnp | grep -E ':(3000|5432|8080|8500|8501|8502|8503|8504)\b'
```

### 8-3. `docker compose up --no-cache` → `unknown flag: --no-cache`
→ `--no-cache`는 **build 전용**이다. `docker compose -f ... build --no-cache` 후 `up -d`.

### 8-4. `.env` 없이 빌드했다
→ `PIP_INDEX_URL` 등이 기본값(공개 PyPI)으로 잡혀 폐쇄망에서 실패한다. `cp .env.example .env` 후 재빌드.

### 8-5. 디스크 부족(`no space left on device`)
→ 오래된 빌드 캐시/이미지 정리: `docker system prune -af` (주의: 사용 안 하는 이미지 삭제).

---

## 9. 부록 — 주요 엔드포인트

| 엔드포인트 | 용도 |
|---|---|
| `POST /v1/chat/completions` | Open WebUI(OpenAI 호환) |
| `POST /v1/agent/query` | 외부 agent 일반 질의 (+ user_id 메모리) |
| `POST /v1/voc/query` | 통합 VOC agent 계약(`similar_voc` 포함, stream 지원) |
| `GET/POST/DELETE /v1/memory/{user_id}` | 사용자 장기 메모리 조회/추가/삭제 |
| `GET /health`, `GET /v1/models` | 상태 확인 |
