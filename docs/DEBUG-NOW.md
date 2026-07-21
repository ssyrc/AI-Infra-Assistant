# 지금 바로 실행할 디버깅 (DEBUG-NOW)

> 커맨드를 손으로 칠 필요 없다. **스크립트 하나만 실행**하면 된다.
> 리포를 받는 방법(WSL git → rsync)은 `docs/CLAUDE-GUIDE.md` 맨 앞 참고.

**전제(폐쇄망 네트워크):** 서버(202.20.183.30)는 HTTP를 직접 못 내보내고, **내부 미러를 포함한
모든 HTTP는 프록시 `202.20.187.241:3128` 를 반드시 거친다.** 그래서 `NO_PROXY`에 미러를 넣으면 안 된다
(`NO_PROXY=localhost,127.0.0.1` 유지). 모든 진단/빌드는 프록시 경유를 전제로 한다.

---

## 현재 상태 (2026-07-21) — 원인 규명됨

- 베이스 3.11 + pip 22.1.2 → **JSON 에러 해결**.
- `debug-net.sh` 로 확인: A) 미러 Content-Type `text/html` ✅, B) 빌드 컨테이너가 프록시로 미러 도달 ✅,
  **C) 빌드와 같은 이미지에서 pip 22.1.2가 fastapi==0.115.8 다운로드 성공 ✅**.
- 즉 **설정·버전·네트워크 다 정상.** `from versions: none` 은 **미러/프록시가 '동시 요청'에 간헐적으로
  빈 응답을 주는 것**이 원인. `docker compose build` 가 8개 이미지를 **병렬**로 빌드하며 인덱스 요청을
  몰아쳐서, 매번 다른 서비스가 랜덤하게 터졌던 것.
- **해결 = 병렬 억제(한 서비스씩) + 자동 재시도.** → `scripts/rebuild.sh` 가 그렇게 하도록 바뀜.
  버전을 바꿀 필요 없음(미러엔 다 있음).

### 그래서 지금 할 것
```bash
bash scripts/rebuild.sh          # 순차 빌드 + 미러 삑나면 자동 재시도
# 캐시 없이 처음부터: NOCACHE=1 bash scripts/rebuild.sh
```

---

## ★ 지금 이걸 먼저 (curl은 되는데 pip만 "from versions: none"일 때)

미러엔 패키지가 다 있는데(curl로 확인됨) **빌드 안 pip만 0개로 보는** 경우다. 버전 문제가 아니다.
원인을 한 번에 가른다:
```bash
bash scripts/debug-net.sh
```
출력이 알려주는 것:
- **A**: 미러가 주는 `Content-Type` (pip는 `text/html`이 아니면 인덱스를 통째로 무시 → "none")
- **B**: 빌드와 같은 컨테이너가 프록시로 미러에 **도달하는지**
- **C**: 그 컨테이너에서 실제 pip가 다운로드되는지

→ A/B/C 출력 붙여주면 근본 원인 확정. **한 번 고치면 모든 패키지가 풀린다**(패키지별로 버전 바꿀 필요 없음).

---

## (참고) 어떤 버전이 미러에 있는지 확인

```bash
cd <repo>
bash scripts/debug-now.sh                     # asyncpg fastapi pydantic docling 확인
# 특정 패키지/버전 존재여부:
bash scripts/debug-now.sh asyncpg==0.31.0 httpx==0.28.1
```
→ 각 패키지에 대해 **미러에 있는 버전 목록**과, 지정한 `==버전`의 존재여부(✅/❌)를 출력한다.
막힌 패키지가 생기면 이걸로 “있는 버전”을 찾아서 알려주면 그 버전으로 핀을 바꿔준다.

---

## 빌드 (표준)

```bash
bash scripts/rebuild.sh          # 사전확인 + 클린 재빌드 + 기동 + health
```
로그에서 `Collecting <pkg> ... Downloading ...` 가 뜨면 정상. `from versions: none` 이 뜨면
그 패키지를 `bash scripts/debug-now.sh <pkg>` 로 확인.

---

## 미러에 아예 없는 버전을 써야 할 때 → vendor 오프라인

미러가 특정 버전을 안 가지고 있으면, 그 휠을 `vendor/`에 넣어 오프라인 설치할 수 있다.
```bash
# 인터넷 되는 WSL에서:
pip download '<pkg>==<ver>' -d vendor/       # 의존성까지: --no-deps 빼고, 특정 wheel만: --no-deps
# rsync로 서버 반영 후:
bash scripts/rebuild.sh
```
> 지금은 asyncpg가 0.31.0으로 미러에 있어 **vendor가 필요 없다.** 다른 패키지에서 필요해지면
> 그때 Dockerfile이 `vendor/`를 find-links로 함께 보도록 배선을 추가한다(문의).

---

## 스크립트 목록 (`scripts/`)
- `debug-now.sh` — 미러에 있는 패키지 버전 확인(프록시 경유).
- `rebuild.sh` — 사전확인 + 클린 재빌드 + 기동 + health.
