ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
# 폐쇄망 빌드: pip 미러/프록시 (기본값=공개 PyPI 직접). compose가 .env에서 전달한다.
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=
ARG BUILD_PROXY=
ARG NO_PROXY=
ENV PIP_INDEX_URL=${PIP_INDEX_URL} PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} PIP_DISABLE_PIP_VERSION_CHECK=1 \
    http_proxy=${BUILD_PROXY} https_proxy=${BUILD_PROXY} HTTP_PROXY=${BUILD_PROXY} HTTPS_PROXY=${BUILD_PROXY} \
    no_proxy=${NO_PROXY} NO_PROXY=${NO_PROXY}
WORKDIR /app
# openssh-client: System/Command MCP가 대상 서버로 ssh 실행할 때 필요.
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*
COPY mcp_servers/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 빌드 프록시를 런타임 컨테이너에 남기지 않는다(내부 서비스 통신이 프록시로 새지 않도록).
ENV http_proxy= https_proxy= HTTP_PROXY= HTTPS_PROXY= no_proxy= NO_PROXY=
COPY shared/ ./shared/
COPY mcp_servers/ ./mcp_servers/
EXPOSE 8001
# 실제 실행할 서버는 docker-compose의 command로 지정 (예: python mcp_servers/manual_mcp/server.py)
