ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
WORKDIR /app
COPY agent_server/requirements.txt agent_server/requirements.txt
RUN pip install --no-cache-dir -r agent_server/requirements.txt
COPY shared/ shared/
COPY agent_server/ agent_server/
WORKDIR /app/agent_server
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
