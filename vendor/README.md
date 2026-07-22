# vendor/ — 폐쇄망 빌드용 오프라인 패키지

폐쇄망 사내 미러(Nexus)는 **최신 pip의 JSON Simple API(PEP 691)** 를 지원하지 않는다.
`python:3.12-slim`이 내장한 최신 pip(24.x)는 인덱스 응답을 `json.loads()` 하다가
아래 에러로 빌드가 깨진다:

```
json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
```

**HTML Simple API만 쓰는 pip < 22.2** 으로 내리면 미러가 정상 동작한다(사내 호스트의 pip가
동작하는 이유와 동일). PEP 691 JSON은 pip 22.2부터 들어갔으므로 **22.1.2**가 마지막 HTML-only
버전이다. 하지만 최신 pip로는 미러에서 그 옛 pip 조차 같은 이유로 못 받으므로 (닭-달걀), 이
디렉터리에 미리 받아둔 pip 휠을 각 Dockerfile이 **오프라인**(`--no-index`)으로 먼저 설치한 뒤,
이후 패키지들을 사내 미러에서 받는다.

> 그리고 이 옛 pip는 **Python 3.12에서 `pkgutil.ImpImporter` 에러로 실행되지 않으므로** 베이스
> 이미지는 **`python:3.11-slim-bullseye`** 이어야 한다(compose·Dockerfile에 반영됨).

## 들어있는 파일

- `pip-22.1.2-py3-none-any.whl` — 순수 파이썬 휠. pip 자체 부트스트랩 전용(위 문제 해결).
- `asyncpg-0.31.0-cp311-...manylinux...x86_64.whl` — 사내 미러가 asyncpg 인덱스 조회 시
  간헐적으로 빈 응답(`from versions: none`)을 줘서 오프라인으로 고정 설치.
  **cp311 + manylinux(x86_64) 휠** — 베이스 이미지(`python:3.11-slim-bullseye`, linux/amd64)와 일치해야 한다.
- `fastapi-0.115.8-py3-none-any.whl` — 순수 파이썬. 같은 이유(미러 간헐 실패)로 오프라인 고정.
- FastAPI/Uvicorn 실행 스택:
  `uvicorn-0.34.0`, `pydantic-2.11.10`, `pydantic_core-2.33.2`, `starlette-0.45.3`,
  `python_multipart-0.0.20`, `anyio`, `annotated_types`, `click`, `h11`, `httptools`,
  `idna`, `python_dotenv`, `pyyaml`, `typing_extensions`, `uvloop`, `watchfiles`,
  `websockets`, `colorama` — 사내 미러에 특정 버전이 없을 때 빌드가 한 패키지씩 막히지 않도록
  함께 고정한다. C 확장 휠은 **CPython 3.11 + linux/amd64** 태그여야 한다.
- MCP 실행 스택:
  `mcp-1.28.1`, `httpx-0.28.1`, `redis-5.2.1`, `httpcore`, `httpx_sse`,
  `jsonschema`, `pydantic_settings`, `pyjwt`, `cryptography`, `cffi`, `sse_starlette-2.2.1`,
  `typing_inspection`, `attrs`, `jsonschema_specifications`, `referencing`, `rpds_py`,
  `certifi`, `pycparser` — 사내 미러에 MCP SDK 또는 하위 의존성이 없을 때를 대비한다.
- `deb/` — MCP 이미지에서 `openssh-client`를 apt 미러 없이 설치하기 위한 Debian bullseye
  linux/amd64 `.deb` 묶음. Dockerfile은 이 디렉터리에 `.deb`가 있으면 `apt-get update`를 하지 않고
  `dpkg --unpack /tmp/vendor/deb/*.deb` 후 `dpkg --configure -a`로 로컬 설치한다.

## 동작 방식 — vendor의 모든 whl은 자동으로 먼저 반영된다

각 Dockerfile은 `pip-*.whl`로 pip를 부트스트랩한 뒤, **`vendor/` 안의 pip 외 `*.whl`을
`--no-index --no-deps`로 순회 설치**한다. 그러면 이후 `pip install -r requirements.txt`
단계에서 그 패키지 본체는 이미 설치돼 있어 미러에 요청을 보내지 않는다. 이후 `pip install`
단계도 `--find-links /tmp/vendor`를 같이 사용하므로, 미러에 없는 패키지가 직접 요구사항으로
다시 등장해도 vendor wheel을 후보로 사용할 수 있다. 필요한 나머지 의존성은 평소처럼 미러에서 받는다.

**즉, 미러가 특정 패키지/버전을 못 주거나(없음 또는 간헐적 빈 응답) 사내망에서 그 원인을 당장
못 고칠 때, 그 패키지의 whl을 이 폴더에 넣기만 하면 Dockerfile을 손대지 않고 바로 해결된다.**

## 휠을 새로 추가/갱신하려면 (인터넷 되는 곳에서 1회)

```bash
# 순수 파이썬 패키지(대부분의 웹 프레임워크 등)
pip download '<pkg>==<버전>' --no-deps -d vendor/

# C 확장이 있는 패키지(예: asyncpg)는 베이스 이미지와 플랫폼이 맞아야 한다.
# 이 리포 베이스는 python:3.11-slim-bullseye, linux/amd64 이므로:
pip download '<pkg>==<버전>' --no-deps -d vendor/ \
  --platform manylinux_2_17_x86_64 --python-version 311 --implementation cp --abi cp311 \
  --only-binary=:all:

# FastAPI/Uvicorn 스택을 의존성까지 묶어서 갱신할 때:
pip download --dest vendor --only-binary=:all: \
  --platform manylinux_2_17_x86_64 --python-version 311 --implementation cp --abi cp311 \
  'fastapi==0.115.8' 'uvicorn[standard]==0.34.0' 'pydantic==2.11.10' 'python-multipart==0.0.20'

# Windows에서 위 명령을 실행하면 Linux 전용 marker 때문에 uvloop이 빠질 수 있어 별도로 받는다.
pip download --dest vendor --only-binary=:all: \
  --platform manylinux_2_17_x86_64 --python-version 311 --implementation cp --abi cp311 \
  'uvloop>=0.14.0,!=0.15.0'

# MCP SDK 스택을 갱신할 때:
pip download --dest vendor --only-binary=:all: \
  --platform manylinux_2_17_x86_64 --python-version 311 --implementation cp --abi cp311 --no-deps \
  'mcp==1.28.1' 'httpx==0.28.1' 'redis==5.2.1'
pip download --dest vendor --only-binary=:all: \
  --platform manylinux_2_17_x86_64 --python-version 311 --implementation cp --abi cp311 \
  'pydantic==2.11.10' 'httpx-sse>=0.4' 'jsonschema>=4.20.0' \
  'pydantic-settings>=2.5.2' 'pyjwt[crypto]>=2.10.1' \
  'sse-starlette==2.2.1' 'typing-inspection>=0.4.1' \
  'httpx==0.28.1' 'redis==5.2.1'
```

폐쇄망 반입만 가능하면, 사내에서 동작하는 pip로 위 명령을 실행해 나온 `.whl`을 이 폴더에 두면 된다.
휠이 없는 패키지는 평소처럼 인덱스에서 설치를 시도하므로, 공개 PyPI 등 정상 미러 환경에서는
이 폴더에 `pip-*.whl` 외엔 없어도 된다.
