# Deployment

The repository includes Render configuration for the FastAPI backend, React/Vite frontend, and a persistent PostgreSQL database.

## Backend

Configure:
- GROQ_API_KEY
- GROQ_MODEL
- GROQ_VISION_MODEL
- CORS_ORIGINS
- MCP_SERVERS when external MCP servers are needed
- R2_* variables when uploaded files should also be persisted in Cloudflare R2

The API is intentionally single-user. There is no registration, login, account database, or per-user data partitioning.

## Persistence

Render PostgreSQL is used when DATABASE_URL is present.

Uploaded files use the configured AGENT_FILE_ROOT. Cloudflare R2 is optional; when configured, uploaded files are mirrored to the bucket.

## Security

Keep ALLOW_SHELL disabled unless it is explicitly required. MCP connectors validate HTTP targets and reject private/local addresses by default. Connector headers and OAuth credentials remain backend-side.

## Frontend

Set VITE_API_URL to the deployed FastAPI base URL, build the frontend, and publish frontend/dist as a static site.

## Verification

Run:

~~~
pytest -q
cd frontend
npm install
npm test
npm run build
~~~
