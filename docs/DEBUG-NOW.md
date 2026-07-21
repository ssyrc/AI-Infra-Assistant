# 지금 바로 실행할 디버깅 (DEBUG-NOW)

> 커맨드를 손으로 칠 필요 없다. **스크립트 하나만 실행**하면 된다.
> 리포를 받는 방법(WSL git → rsync)은 `docs/CLAUDE-GUIDE.md` 맨 앞 참고.

---

## 현재 증상 (2026-07-21)

- 베이스 3.11 + pip 22.1.2 로 **JSON 에러는 해결됨**.
- 그런데 `pip install`이 `ERROR: Could not find a version that satisfies the requirement <pkg> (from versions: none)`.
- **호스트에서도** `pip install asyncpg==0.30.0` 이 동일하게 실패 → 우리 Dockerfile 문제 아님. 미러가 인덱스를 안 주는 것.
- 간헐적(직전 빌드엔 받아졌음) → **내부 미러를 외부 프록시로 태우는** 설정이 유력.

---

## 지금 실행 (이거 하나만)

```bash
bash scripts/debug-now.sh | tee debug-now.out
```

→ 끝나면 화면 출력(또는 `debug-now.out` 파일 내용)을 **그대로 복사해서 붙여줘.**
스크립트가 미러를 `프록시 통해서(①)` vs `프록시 없이 직접(②)` 로 asyncpg/fastapi/pydantic 각각
조회해서, 파일 링크가 몇 개 잡히는지 보여주고 판정 가이드까지 출력한다.

---

## 결과에 따른 다음 조치 (스크립트로)

| debug-now 결과 | 실행할 스크립트 |
|---|---|
| **②(직접)만** 링크 뜸 | `bash scripts/set-noproxy-direct.sh` → `bash scripts/rebuild.sh` |
| **①②(둘 다)** 링크 뜸 | 일시장애일 뿐 → `bash scripts/rebuild.sh` (재시도) |
| **①②(둘 다)** 링크 없음 | 인덱스 URL이 잘못됨(files 프록시). 미러 관리자에게 pypi simple proxy URL 문의 (아래) |

### 인덱스 URL이 잘못된 경우 (둘 다 빔)
`proxy-pypi-files.pythonhosted.org` 는 파일 프록시라 버전 목록(simple index)을 안 줄 수 있다.
Nexus에 pypi.org 를 프록시하는 다른 repo가 있는지 찾아본다:
```bash
curl -sS --noproxy '*' "http://repository.samsungds.net/service/rest/v1/repositories" 2>/dev/null \
  | tr ',' '\n' | grep -iE '"(name|url)".*pypi'
```
여기서 나오는 simple 인덱스 URL을 `.env` 의 `PIP_INDEX_URL` 에 넣고 `bash scripts/rebuild.sh`.

---

## 스크립트 목록 (`scripts/`)
- `debug-now.sh` — 현재 막힌 지점 진단(위).
- `set-noproxy-direct.sh` — `.env` 의 NO_PROXY 에 미러 호스트 추가(프록시 우회).
- `rebuild.sh` — 사전확인 + 클린 재빌드 + 기동 + health.
