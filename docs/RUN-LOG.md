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

---

## 다음 항목은 이어서 여기 아래에 추가
