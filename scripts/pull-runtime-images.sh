#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

DOCKERHUB_NS="${DOCKERHUB_NS:-ellie0}"
TAG="${1:-${TAG:-main-$(git rev-parse --short HEAD)}}"

pull_and_tag() {
  local repo="$1"
  local local_tag="$2"
  local remote="${DOCKERHUB_NS}/${repo}:${TAG}"

  docker pull "$remote"
  docker tag "$remote" "$local_tag"
}

pull_and_tag ai-infra-assistant-db-init ai-infra-assistant-db-init:latest
pull_and_tag ai-infra-assistant-mock-vllm ai-infra-assistant-mock-vllm:latest
pull_and_tag ai-infra-assistant-mcp ai-infra-assistant-mcp:dev
pull_and_tag ai-infra-assistant-agent-server ai-infra-assistant-agent-server:latest
pull_and_tag ai-infra-assistant-admin-console ai-infra-assistant-admin-console:latest
pull_and_tag ai-infra-assistant-pgvector pgvector/pgvector:pg16
pull_and_tag ai-infra-assistant-postgres postgres:16-alpine
pull_and_tag ai-infra-assistant-open-webui ghcr.io/open-webui/open-webui:v0.6.5

echo "pulled and retagged runtime images: ${TAG}"
