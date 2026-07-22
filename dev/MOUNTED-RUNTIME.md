# Mounted Runtime Development

`docker-compose.dev.yml` builds only the Python/OS package environment for the app services.
The application source is bind-mounted from the host, so ordinary code changes do not require a Docker rebuild.

Mounted source paths:

- `agent_server/` -> `/app/agent_server`
- `mcp_servers/` -> `/app/mcp_servers`
- `admin_console/` -> `/app/admin_console`
- `shared/` -> `/app/shared`
- `dev/mock_vllm.py` -> `/app/mock_vllm.py`

Run the stack:

```bash
docker compose -f docker-compose.dev.yml build
docker compose -f docker-compose.dev.yml up -d
```

For code-only changes, do not rebuild the images. The `agent-server`, `admin-console`, and `mock-vllm` services use uvicorn reload. The MCP services use `watchfiles` to restart their Python process when mounted code changes.

If you need to force a restart:

```bash
docker compose -f docker-compose.dev.yml restart agent-server manual-mcp command-mcp voc-mcp system-mcp admin-console
```

Rebuild only when one of these changes:

- `requirements.txt`
- `vendor/`
- `vendor/deb/`
- a Dockerfile
- the Python base image or apt/pip mirror settings
