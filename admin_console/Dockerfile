FROM python:3.12-slim
WORKDIR /app
COPY admin_console/backend/requirements.txt admin_console/backend/requirements.txt
COPY admin_console/backend/requirements-docling.txt admin_console/backend/requirements-docling.txt
RUN pip install --no-cache-dir -r admin_console/backend/requirements.txt \
    && pip install --no-cache-dir -r admin_console/backend/requirements-docling.txt
COPY shared/ shared/
COPY mcp_servers/system_mcp/whitelist.py mcp_servers/system_mcp/whitelist.py
COPY admin_console/ admin_console/
WORKDIR /app/admin_console/backend
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
