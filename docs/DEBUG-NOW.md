# 지금 바로 실행할 디버깅 (DEBUG-NOW)

> 이 문서는 **현재 막힌 지점**을 확인하기 위한 즉시 실행 명령만 담는다. 상황이 바뀔 때마다 갱신된다.
> 전체 실행 절차는 `docs/CLAUDE-GUIDE.md`, 배경 원리는 그 3장 참고.

---

## 현재 증상 (2026-07-21)

- 베이스 3.11 + pip 22.1.2 로 **JSON 에러는 해결됨**.
- 그런데 `pip install`이 `ERROR: Could not find a version that satisfies the requirement <pkg> (from versions: none)` 로 실패.
- **호스트에서도** `pip install asyncpg==0.30.0` 이 동일하게 `from versions: none` → 우리 Dockerfile 문제 아님. 미러가 인덱스를 안 주는 것.
- 간헐적(직전 빌드엔 받아졌음) → **내부 미러를 외부 프록시(202.20.187.241:3128)로 태우는** 설정이 유력한 원인.

---

## STEP 1 — 미러가 목록을 주는지 확인 (프록시 통함 vs 직접)

서버에서 그대로 실행하고 **양쪽 출력을 붙여줘**:

```bash
# ① 프록시 '통해서' (지금 방식)
http_proxy=http://202.20.187.241:3128 https_proxy=http://202.20.187.241:3128 \
  curl -sS "http://repository.samsungds.net/repository/proxy-pypi-files.pythonhosted.org/simple/asyncpg/" | head -30
echo "===== ↑프록시 통함 / ↓프록시 없이 직접 ====="
# ② 프록시 '없이' 직접
curl -sS --noproxy '*' "http://repository.samsungds.net/repository/proxy-pypi-files.pythonhosted.org/simple/asyncpg/" | head -30
```

### 결과 해석
| 결과 | 의미 | 다음 조치 |
|---|---|---|
| ②(직접)에 `asyncpg-*.whl` 링크 뜨고 ①(프록시)은 빔 | 내부 미러를 프록시로 타면 깨짐 | **STEP 2-A** (NO_PROXY에 미러 추가) |
| ①②(둘 다) 링크 뜸 | 프록시 문제 아님, 일시장애/캐시 | 그냥 재빌드 재시도 |
| ①②(둘 다) 빔 (링크 없음) | 이 URL이 files 프록시라 simple 인덱스가 아님 | 미러 관리자에게 **pypi.org simple proxy** URL 문의 |

---

## STEP 2-A — (②가 정답일 때) 내부 미러를 프록시 없이 직접 접근

`.env`에서 `NO_PROXY`에 미러 호스트 추가:
```bash
# .env 안에서 이 줄을 이렇게:
NO_PROXY=repository.samsungds.net,localhost,127.0.0.1
```
그리고 재빌드:
```bash
docker builder prune -af
docker compose -f docker-compose.dev.yml build --no-cache
docker compose -f docker-compose.dev.yml up -d
```

빌드 로그에서 `Collecting asyncpg==0.30.0 ... Downloading ...` 가 뜨면 통과.

---

## STEP 2-C — (①②가 둘 다 빔일 때) 인덱스 URL이 잘못됨

`proxy-pypi-files.pythonhosted.org` 는 **파일(files.pythonhosted.org) 프록시**라 버전 목록(simple index)을 안 줄 수 있다.
Nexus에 **pypi.org 를 프록시하는 별도 repo**(보통 이름에 `pypi` 있고 `files` 없음)가 있는지 확인:

```bash
# Nexus repo 목록에서 pypi 계열 찾기 (인증 필요할 수 있음)
curl -sS --noproxy '*' "http://repository.samsungds.net/service/rest/v1/repositories" 2>/dev/null \
  | tr ',' '\n' | grep -iE '"(name|url)".*pypi'
```
여기서 나오는 **simple 인덱스용 URL**을 `.env`의 `PIP_INDEX_URL`에 넣는다.
