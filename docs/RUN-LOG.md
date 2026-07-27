# 실행 커맨드 로그 (Agent 기동 & 테스트)

> 회사 서버에서 실제로 실행한 커맨드 기준. 새 작업을 할 때마다 이 파일에 이어서 추가한다.

## 1. 이미지 빌드/전송 (오프라인 배포)

인터넷 되는 곳에서 빌드 후 Docker Hub(`ellie0/*`)로 push, 폐쇄망 서버로는 tar로 옮김.

```bash
docker info | grep "Docker Root Dir"
```
도커 저장 위치 확인 (D 드라이브: `/mnt/d/docker-data`).

```bash
RUNTIME_TAG=main-10bc550

docker save \
  ellie0/ai-infra-assistant-db-init:$RUNTIME_TAG \
  ellie0/ai-infra-assistant-mock-vllm:$RUNTIME_TAG \
  ellie0/ai-infra-assistant-mcp:$RUNTIME_TAG \
  ellie0/ai-infra-assistant-agent-server:$RUNTIME_TAG \
  ellie0/ai-infra-assistant-admin-console:$RUNTIME_TAG \
  ellie0/ai-infra-assistant-pgvector:$RUNTIME_TAG \
  ellie0/ai-infra-assistant-postgres:$RUNTIME_TAG \
  ellie0/ai-infra-assistant-open-webui:$RUNTIME_TAG \
  | gzip > /mnt/d/ai-infra-images.tar.gz
```
런타임 이미지 8종을 tar.gz로 저장.

```bash
rsync -avz --progress /mnt/d/ai-infra-images.tar.gz \
  yr9.choi@202.20.185.100:/home/gpu1/yr9.choi/05_halo/
```
폐쇄망 서버로 전송.

## 2. 폐쇄망 서버에서 로드 & 기동

```bash
docker load < ai-infra-images.tar.gz
docker images | grep ai-infra-assistant   # 로드된 태그 확인
```

```bash
docker compose -f docker-compose.dev.yml up -d --no-build
```

**에러**: `No such image: ai-infra-assistant-admin-console:latest`
원인: load된 이미지는 `ellie0/...:main-10bc550` 태그인데, compose는 로컬 태그(`:latest`)를 찾음.

**해결**:
```bash
bash scripts/retag-runtime-images.sh main-10bc550
docker compose -f docker-compose.dev.yml up -d --no-build
```
8개 이미지를 compose용 로컬 태그로 재태깅.

## 3. 기동 확인

```bash
docker compose -f docker-compose.dev.yml ps
curl http://localhost:8500/health
```
→ 전체 서비스 Up, `{"status":"ok",...}` 확인됨.

## 4. 코드만 바뀔 때 (재빌드 없이 재기동)

```bash
git pull origin main
bash scripts/restart-mounted.sh
```

## 5. 코드 반영 (WSL → rsync → 폐쇄망 서버)

로컬 수정 없이 원격 main을 그대로 받아서 서버로 올리는 방식.

```bash
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
```

```bash
rsync -avz --delete --progress /home/yrc/AI-Infra-Assistant/ \
  yr9.choi@202.20.185.100:/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant/
```

## 6. 호스트 포트 8500번대로 재배치 (확인됨)

8080/8100/8200/3000/3001/5432 대신 8500부터 순서대로 배정
(`docker-compose.dev.yml`/`docker-compose.yml`/`.env.example`).

```bash
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml ps
```
→ 관리자 콘솔 `:8501`, 사용자 웹 `:8502` 접속 확인됨.

## 7. admin_console vendor 파일 받기 (진행 중)

```bash
unset CURL_CA_BUNDLE SSL_CERT_FILE SSL_CERT_DIR
export https_proxy=http://202.20.187.241:3128 http_proxy=http://202.20.187.241:3128
cd admin_console/frontend/vendor
curl -o react.production.min.js https://unpkg.com/react@18/umd/react.production.min.js
curl -o react-dom.production.min.js https://unpkg.com/react-dom@18/umd/react-dom.production.min.js
curl -o babel.min.js https://unpkg.com/@babel/standalone/babel.min.js
```
1차 시도: 인증서 경로 에러 → `unset`으로 해결. 2차 시도: 파일은 받아졌지만 브라우저에서
`Uncaught SyntaxError: Unexpected identifier 'to'` → 프록시 차단 페이지가 저장된 것으로 추정,
프록시 경유 재다운로드 확인 중.

---

## 다음 항목은 이어서 여기 아래에 추가
