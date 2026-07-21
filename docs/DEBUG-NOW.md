# 지금 바로 실행할 디버깅 (DEBUG-NOW)

> 커맨드를 손으로 칠 필요 없다. **스크립트 하나만 실행**하면 된다.
> 리포를 받는 방법(WSL git → rsync)은 `docs/CLAUDE-GUIDE.md` 맨 앞 참고.

**전제(폐쇄망 네트워크):** 서버(202.20.183.30)는 HTTP를 직접 못 내보내고, **내부 미러를 포함한
모든 HTTP는 프록시 `202.20.187.241:3128` 를 반드시 거친다.** 그래서 `NO_PROXY`에 미러를 넣으면 안 된다
(`NO_PROXY=localhost,127.0.0.1` 유지). 모든 진단/빌드는 프록시 경유를 전제로 한다.

---

## 현재 상태 (2026-07-21)

- 베이스 3.11 + pip 22.1.2 → **JSON 에러 해결**, 미러에서 패키지 다운로드 정상 동작 확인.
- 남은 문제는 **특정 버전 핀이 미러에 없는 것**뿐.
  - 예: `asyncpg==0.30.0` 은 미러에 **없고**, `asyncpg==0.31.0` 은 **있음** → 전부 `0.31.0`으로 정렬함.
- 다른 패키지도 같은 일이 나면(“from versions: none”), 미러에 **있는 버전**으로 핀을 바꾸면 된다.

---

## 어떤 버전이 미러에 있는지 확인 (이 스크립트 하나)

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
