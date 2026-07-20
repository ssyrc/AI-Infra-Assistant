ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
WORKDIR /app
# openssh-client: System/Command MCP가 대상 서버로 ssh 실행할 때 필요.
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*
COPY mcp_servers/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/ ./shared/
COPY mcp_servers/ ./mcp_servers/
EXPOSE 8001
# 실제 실행할 서버는 docker-compose의 command로 지정 (예: python mcp_servers/manual_mcp/server.py)
