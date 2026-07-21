# vendor/ — 폐쇄망 빌드용 오프라인 pip 휠

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
> 이미지는 **`python:3.11-slim`** 이어야 한다(compose·Dockerfile에 반영됨).

## 들어있는 파일

- `pip-22.1.2-py3-none-any.whl` — 순수 파이썬 휠(py3-none-any)이라 이미지에 그대로 쓰인다.

## 휠을 직접 갱신/교체하려면 (인터넷 되는 곳에서 1회)

```bash
pip download pip==22.1.2 --no-deps -d vendor/
```

폐쇄망 반입만 가능하면, 사내에서 동작하는 pip로 위 명령을 실행해 나온 `.whl`을 이 폴더에 두면 된다.
`pip-*.whl` 이름이면 Dockerfile이 자동으로 집어서 오프라인 설치한다. 파일이 없으면 인덱스에서
직접 설치를 시도하므로 공개 PyPI 등 정상 미러 환경에서는 이 폴더가 비어 있어도 된다.
