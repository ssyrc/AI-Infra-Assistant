# Company Docker Hub Runtime Runbook

This guide is for running AI-Infra-Assistant on the company agent server when Docker Hub pull is available.

## Quick Start

회사 agent 서버에서 아래 순서대로 실행한다. Docker Hub 이미지는 직접 `docker pull`로 받고, 그 다음 스크립트는 이미 받은 이미지를 compose용 로컬 태그로 맞추기만 한다.

```bash
cd /opt
git clone https://github.com/ssyrc/AI-Infra-Assistant.git
cd /opt/AI-Infra-Assistant
```

이미 clone 되어 있으면:

```bash
cd /opt/AI-Infra-Assistant
git pull origin main
```

`.env`가 있고 사내 미러 값이 들어 있으면, 이 Docker Hub pull 방식에서는 public registry로 맞춘다:

```bash
grep -q '^REGISTRY_DOCKERHUB=' .env && sed -i 's|^REGISTRY_DOCKERHUB=.*|REGISTRY_DOCKERHUB=docker.io|' .env || echo 'REGISTRY_DOCKERHUB=docker.io' >> .env
grep -q '^REGISTRY_GHCR=' .env && sed -i 's|^REGISTRY_GHCR=.*|REGISTRY_GHCR=ghcr.io|' .env || echo 'REGISTRY_GHCR=ghcr.io' >> .env
```

Docker Hub에서 runtime 이미지들을 직접 pull한다:

```bash
RUNTIME_TAG=main-10bc550

docker pull ellie0/ai-infra-assistant-db-init:$RUNTIME_TAG
docker pull ellie0/ai-infra-assistant-mock-vllm:$RUNTIME_TAG
docker pull ellie0/ai-infra-assistant-mcp:$RUNTIME_TAG
docker pull ellie0/ai-infra-assistant-agent-server:$RUNTIME_TAG
docker pull ellie0/ai-infra-assistant-admin-console:$RUNTIME_TAG
docker pull ellie0/ai-infra-assistant-pgvector:$RUNTIME_TAG
docker pull ellie0/ai-infra-assistant-postgres:$RUNTIME_TAG
docker pull ellie0/ai-infra-assistant-open-webui:$RUNTIME_TAG
```

이미지 태그를 compose용으로 맞추고, 빌드 없이 실행한다:

```bash
bash scripts/pull-runtime-images.sh "$RUNTIME_TAG"
docker compose -f docker-compose.dev.yml up -d --no-build
docker compose -f docker-compose.dev.yml ps
curl http://localhost:8500/health
```

정상 응답:

```json
{"status":"ok","model":"mock-llm"}
```

이후 코드만 바뀌면:

```bash
cd /opt/AI-Infra-Assistant
git pull origin main
bash scripts/restart-mounted.sh
```

Current runtime image tag:

```bash
RUNTIME_TAG=main-10bc550
```

The runtime images contain only the Python and OS package environment. Application code is not baked into the images. The code is loaded from the local repository directory through bind mounts in `docker-compose.dev.yml`.

Use the runtime image tag above even if the Git `main` branch has newer documentation or script commits. Rebuild and push a new runtime image tag only when dependencies, vendor files, Dockerfiles, or base image settings change.

## 1. Prepare The Repository

Use `/opt/AI-Infra-Assistant` as the server-side working directory.

If the repository is not cloned yet:

```bash
cd /opt
git clone https://github.com/ssyrc/AI-Infra-Assistant.git
cd /opt/AI-Infra-Assistant
```

If the repository already exists:

```bash
cd /opt/AI-Infra-Assistant
git pull origin main
```

If GitHub access is unavailable from the server, copy the repository folder to `/opt/AI-Infra-Assistant` by another approved transfer method.

## 2. Check Docker Access

```bash
docker version
docker compose version
docker login
```

If `docker` requires root permission on the server, run the commands below with `sudo`, for example `sudo docker pull ...`.

## 3. Keep Registry Settings Public For This Flow

Do not run `scripts/rebuild.sh` for this Docker Hub pull flow.

If there is no `.env` file, you can leave it absent because `docker-compose.dev.yml` has public registry defaults.

If `.env` exists and contains company mirror registry values, set these two values to public registries before `docker compose up`:

```bash
grep -q '^REGISTRY_DOCKERHUB=' .env && sed -i 's|^REGISTRY_DOCKERHUB=.*|REGISTRY_DOCKERHUB=docker.io|' .env || echo 'REGISTRY_DOCKERHUB=docker.io' >> .env
grep -q '^REGISTRY_GHCR=' .env && sed -i 's|^REGISTRY_GHCR=.*|REGISTRY_GHCR=ghcr.io|' .env || echo 'REGISTRY_GHCR=ghcr.io' >> .env
```

The pip and apt mirror variables do not matter for `up -d --no-build` because no image build is performed.

## 4. Pull Runtime Images From Docker Hub

Run each pull explicitly. The retag script in the next step does not pull images.

```bash
RUNTIME_TAG=main-10bc550

docker pull ellie0/ai-infra-assistant-db-init:$RUNTIME_TAG
docker pull ellie0/ai-infra-assistant-mock-vllm:$RUNTIME_TAG
docker pull ellie0/ai-infra-assistant-mcp:$RUNTIME_TAG
docker pull ellie0/ai-infra-assistant-agent-server:$RUNTIME_TAG
docker pull ellie0/ai-infra-assistant-admin-console:$RUNTIME_TAG
docker pull ellie0/ai-infra-assistant-pgvector:$RUNTIME_TAG
docker pull ellie0/ai-infra-assistant-postgres:$RUNTIME_TAG
docker pull ellie0/ai-infra-assistant-open-webui:$RUNTIME_TAG
```

## 5. Retag Images For Docker Compose

`docker-compose.dev.yml` uses local image names such as `ai-infra-assistant-agent-server:latest` and `ai-infra-assistant-mcp:dev`. After the Docker Hub pulls, retag the already-pulled images:

```bash
bash scripts/pull-runtime-images.sh "$RUNTIME_TAG"
```

Expected final line:

```text
retagged already-pulled runtime images: main-10bc550
```

## 6. Start The Stack Without Build

```bash
docker compose -f docker-compose.dev.yml up -d --no-build
```

Check service status:

```bash
docker compose -f docker-compose.dev.yml ps
```

Check the agent health endpoint:

```bash
curl http://localhost:8500/health
```

Expected response:

```json
{"status":"ok","model":"mock-llm"}
```

## 7. Service URLs

Replace `SERVER_IP` with the agent server IP.

```text
Agent API      http://SERVER_IP:8500
Admin Console  http://SERVER_IP:8080
Open WebUI     http://SERVER_IP:3000
Manual MCP     http://SERVER_IP:8501/mcp
Command MCP    http://SERVER_IP:8502/mcp
VOC MCP        http://SERVER_IP:8503/mcp
System MCP     http://SERVER_IP:8504/mcp
PostgreSQL     SERVER_IP:5432
```

## 8. Code-Only Updates

For normal source code changes, do not rebuild or repull images.

```bash
cd /opt/AI-Infra-Assistant
git pull origin main
bash scripts/restart-mounted.sh
```

The code directories are mounted into the containers:

```text
./agent_server   -> /app/agent_server
./mcp_servers    -> /app/mcp_servers
./admin_console  -> /app/admin_console
./shared         -> /app/shared
```

## 9. When To Pull New Runtime Images

Pull a new runtime image tag only when one of these changes:

```text
requirements.txt
vendor/
vendor/deb/
Dockerfiles
Python base image
apt or pip mirror settings used during image build
```

When a new runtime tag is published, replace `RUNTIME_TAG=main-10bc550` with the new tag and repeat steps 4 through 6.

## 10. Troubleshooting

If `pull access denied` appears:

```bash
docker login
```

Then repeat the failed `docker pull`.

If `scripts/pull-runtime-images.sh` says `missing image`, the corresponding Docker Hub image was not pulled yet. Pull the exact image shown in the error message, then rerun the script.

If `docker compose up -d --no-build` tries to build images, stop and check that the command includes `--no-build`.

If compose tries to pull from a company mirror instead of the local Docker Hub tags, check `.env`:

```bash
grep '^REGISTRY_' .env
```

For this flow, use:

```bash
REGISTRY_DOCKERHUB=docker.io
REGISTRY_GHCR=ghcr.io
```

If health returns an empty reply immediately after restart, wait a few seconds and retry:

```bash
for i in {1..30}; do curl -fsS http://localhost:8500/health && echo && break; sleep 1; done
```
