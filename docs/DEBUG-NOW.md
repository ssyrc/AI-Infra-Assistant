# 지금 바로 실행할 디버깅 (DEBUG-NOW)

> 커맨드를 손으로 칠 필요 없다. **스크립트 하나만 실행**하면 된다.
> 리포를 받는 방법(WSL git → rsync)은 `docs/CLAUDE-GUIDE.md` 맨 앞 참고.

**전제(폐쇄망 네트워크):** 서버(202.20.183.30)는 HTTP를 직접 못 내보내고, **내부 미러를 포함한
모든 HTTP는 프록시 `202.20.187.241:3128` 를 반드시 거친다.** 그래서 `NO_PROXY`에 미러를 넣으면 안 된다
(`NO_PROXY=localhost,127.0.0.1` 유지).

---

## apt 미러 설정

MCP 이미지는 `openssh-client` 설치가 필요하다. `python:3.11-slim` 기본 sources는
`deb.debian.org`를 바라보므로 폐쇄망에서 실패할 수 있다. `.env`에 아래 값을 둔다.

```bash
APT_MIRROR=http://repository.samsungds.net/repository/proxy-apt-mirror.kakao.com-debian
```

`mcp_servers/Dockerfile`은 빌드 중 컨테이너의 `VERSION_CODENAME`을 읽어 `trixie`, `bookworm`,
`bullseye` 등 현재 베이스 이미지에 맞는 apt source를 만들고, 기존 외부 Debian source 파일을 제거한다.
apt 요청도 `BUILD_PROXY=http://202.20.187.241:3128`를 그대로 탄다.

---

## 현재 상태 (2026-07-21) — 해결됨: asyncpg/fastapi를 vendor로 고정

- 베이스 3.11 + pip 22.1.2 → JSON 에러 해결.
- `debug-net.sh` 로 확인: 미러·프록시·pip 설정 자체는 정상(빌드와 같은 이미지에서 pip가
  fastapi==0.115.8을 성공적으로 받음). 그런데 실제 `docker compose build`(8개 이미지 병렬)에서는
  `asyncpg`/`fastapi` 요청이 **간헐적으로 빈 응답**(`from versions: none`)을 받았다.
- **해결**: 가장 자주 걸리는 두 패키지(`asyncpg==0.31.0`, `fastapi==0.115.8`)의 whl을
  `vendor/`에 미리 받아두고, 모든 Dockerfile이 **패키지 본체를 오프라인으로 먼저 설치**하도록 배선했다.
  그러면 이 두 패키지 본체는 **미러 요청 자체를 안 한다** → 간헐 실패 원인이 원천 제거됨.
  필요한 의존성과 나머지 패키지는 평소처럼 미러에서 받는다. `rebuild.sh`는 순차 빌드하되 실패 시 바로 멈춘다.

### vendor/ 동작 방식 (자동, Dockerfile 수정 불필요)
`vendor/` 안의 `*.whl` 은 각 Dockerfile 빌드 초반에 **의존성 없이 본체만 오프라인 설치**된다.
이후 `pip install -r requirements.txt`는 그 패키지를 "이미 만족"으로 보고 건너뛴다.
→ **앞으로 다른 패키지가 또 미러에서 말썽이면, 그 whl을 vendor/에 추가하기만 하면 된다**
(Dockerfile을 다시 손댈 필요 없음). 자세한 건 `vendor/README.md`.

### 그래서 지금 할 것
```bash
bash scripts/rebuild.sh          # 순차 빌드 + 첫 실패 즉시 중단
# 캐시 없이 처음부터: NOCACHE=1 bash scripts/rebuild.sh
```
로그에서 asyncpg/fastapi 설치 단계가 `Looking in indexes` 없이(즉 미러 요청 없이) 바로
"Successfully installed" 되면 정상 반영된 것.

---

## 다른 패키지가 또 "from versions: none" 이면

1) 미러에 있는 버전 확인:
```bash
bash scripts/debug-net.sh          # A/B/C 자동 진단(설정 자체가 문제인지)
bash scripts/debug-now.sh <pkg>==<버전>   # 그 버전이 미러에 있는지 ✅/❌
```
2) 있는데도 간헐적으로 계속 실패하면 → 그 패키지 whl을 vendor에 추가(아래).
3) 미러에 아예 없으면 → 있는 버전으로 requirements의 핀을 바꾸거나, PyPI 등에서 받은 whl을 vendor에 추가.

### whl을 vendor에 추가하는 법 (인터넷 되는 WSL 등에서 1회)
```bash
# 순수 파이썬 패키지
pip download '<pkg>==<버전>' --no-deps -d vendor/

# C 확장 패키지(예: asyncpg류) — 베이스가 python:3.11-slim, linux/amd64 이므로 플랫폼 지정 필수
pip download '<pkg>==<버전>' --no-deps -d vendor/ \
  --platform manylinux_2_17_x86_64 --python-version 311 --implementation cp --abi cp311 \
  --only-binary=:all:
```
받은 뒤 rsync로 서버에 반영하고 `bash scripts/rebuild.sh`.

---

## 스크립트 목록 (`scripts/`)
- `debug-now.sh` — 미러에 있는 패키지 버전 확인(프록시 경유).
- `debug-net.sh` — curl은 되는데 pip만 실패할 때 A(Content-Type)/B(도달)/C(실제 다운로드) 진단.
- `rebuild.sh` — 사전확인 + 순차 빌드(실패 시 즉시 중단) + 기동 + health.
