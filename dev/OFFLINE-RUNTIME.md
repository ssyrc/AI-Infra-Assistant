# Offline Runtime Images

Docker Hub stores images through `docker push`, not `docker save` tar files.
Use this flow when the agent server cannot pull images directly.

On an internet-connected machine:

```bash
TAG=main-<commit>
git pull origin main

docker pull ellie0/ai-infra-assistant-db-init:$TAG
docker pull ellie0/ai-infra-assistant-mock-vllm:$TAG
docker pull ellie0/ai-infra-assistant-mcp:$TAG
docker pull ellie0/ai-infra-assistant-agent-server:$TAG
docker pull ellie0/ai-infra-assistant-admin-console:$TAG
docker pull ellie0/ai-infra-assistant-pgvector:$TAG
docker pull ellie0/ai-infra-assistant-postgres:$TAG
docker pull ellie0/ai-infra-assistant-open-webui:$TAG

bash scripts/pull-runtime-images.sh "$TAG"
TAG="$TAG" bash scripts/save-runtime-images.sh
```

Copy the generated `dist/ai-infra-assistant-runtime-${TAG}.tar` file and this repository to the closed-network server.

On the closed-network server:

```bash
cd /opt/AI-Infra-Assistant
docker load -i ai-infra-assistant-runtime-<tag>.tar
docker compose -f docker-compose.dev.yml up -d --no-build
curl http://localhost:8500/health
```

For code-only updates after that:

```bash
git pull origin main
bash scripts/restart-mounted.sh
```

Rebuild and redistribute the runtime images only when dependencies, vendor files, Dockerfiles, or base image/mirror settings change.
