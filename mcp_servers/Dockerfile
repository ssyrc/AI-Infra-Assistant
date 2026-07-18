FROM python:3.12-slim
WORKDIR /app
COPY mcp_servers/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/ ./shared/
COPY mcp_servers/ ./mcp_servers/
EXPOSE 8001
# 실제 실행할 서버는 docker-compose의 command로 지정 (예: python mcp_servers/manual_mcp/server.py)
