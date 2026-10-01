# Personal AI Assistant

A self-hosted, single-user AI assistant with a React/Vite frontend and a Python/FastAPI backend.

It combines conversational chat with persistent history, semantic memory, document retrieval (RAG), file and image input, local tools, web search, and optional MCP connectors. The application is designed to run as one private assistant instance rather than as a multi-user SaaS product.

## What it does

- **Conversational chat** powered by Groq-hosted models.
- **Persistent conversations** with multiple chat sessions, titles, history, edit/resend, regenerate, and clear/delete controls.
- **Semantic memory** using FastEmbed, plus explicit memory save/search/delete flows.
- **Document RAG** for uploaded PDFs, DOCX, TXT, Markdown, CSV, JSON, XML, and supported images.
- **Multimodal input** for image attachments and vision-capable model requests.
- **Local tools** including a safe calculator and sandboxed file operations.
- **Optional shell execution**, disabled by default and restricted to an allowlist when enabled.
- **Web search** through DDGS.
- **MCP connectors** with tool discovery, relevance filtering, per-connector allowlists, diagnostics, Streamable HTTP/SSE transports, and OAuth support.
- **Persistent background jobs** stored in PostgreSQL and executed by a single in-process worker.
- **Single-account authentication** with PBKDF2-SHA256 password hashing and HttpOnly session cookies.
- **Responsive web UI** built with React/Vite, with mobile-oriented controls and Capacitor Android build support.

## Architecture

The application is split into a browser frontend and a Python API/runtime.

```text
React / Vite
     |
     | HTTP / SSE
     v
FastAPI backend
     |
     +--------------------+
     |                    |
     v                    v
PostgreSQL           Agent runtime
     |                    |
     |              +-----+-----+----------+
     |              |           |          |
     |              v           v          v
     |           Groq       Memory/RAG   Tools
     |                                    |
     |                              +-----+------+
     |                              |            |
     |                              v            v
     |                           Local       MCP servers
     |                           tools
     |
     +--> durable chat jobs
              |
              v
       in-process worker
```

### Request flow

Simple conversational requests can be handled directly by the API. Tool-bearing or attachment-heavy work is dispatched as a durable PostgreSQL-backed job.

The agent:

1. Builds bounded conversation context.
2. Retrieves relevant memory or document context when requested.
3. Selects relevant local/MCP tools from their advertised metadata.
4. Executes tool calls with bounded iterations and retry/recovery handling.
5. Returns and persists the final answer.

There is no application-level system prompt dependency in the current runtime. Retrieved memory, RAG material, and tool output are passed as reference/context data rather than as hidden system instructions.

## Project structure

```text
.
├── agent.py              # Agent runtime, context, tools, retries, streaming
├── orchestration.py      # Task classification and tool-selection logic
├── api.py                # FastAPI REST/SSE API
├── auth.py               # Single-account authentication and sessions
├── db.py                 # PostgreSQL connection helpers
├── tools.py              # Calculator, web search, files, history, memory
├── memory.py             # Semantic memory and RAG
├── document_parser.py    # Document extraction and OCR handling
├── multimodal.py         # Uploads, images and optional R2 storage
├── preferences.py        # Single-instance preferences
├── jobs.py               # Persistent PostgreSQL job state
├── task_queue.py         # In-process background worker
├── tasks.py              # Chat-job execution and retries
├── connectors.py         # Persistent MCP connector configuration
├── mcp_registry.py       # MCP server configuration/registry
├── mcp_client.py         # MCP discovery and tool execution
├── mcp_oauth.py          # MCP OAuth flow and token storage
├── config.py             # Environment/configuration
├── render.yaml           # Render deployment definition
├── frontend/             # React/Vite web application
└── tests/                # Backend and frontend tests
```

## Requirements

### Backend

- Python 3.13 is used by the included Render configuration.
- PostgreSQL.
- A Groq API key.
- Internet access for Groq, web search, and any remote MCP connectors you configure.

### Frontend

- Node.js/npm.
- The frontend uses React 19 and Vite.
- Vitest is used for frontend tests.
- Capacitor is included for Android builds.

## Local setup

### 1. Clone the repository

```bash
git clone <your-repository-url>
cd Personal-AI-Assistant
```

### 2. Create a Python environment

```bash
python -m venv .venv
source .venv/bin/activate
```

On Windows:

```powershell
.venv\Scripts\Activate.ps1
```

### 3. Install backend dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

At minimum:

```bash
export DATABASE_URL="postgresql://USER:PASSWORD@HOST:5432/DATABASE"
export GROQ_API_KEY="your-groq-api-key"
```

For local development, you can also configure the model explicitly:

```bash
export GROQ_MODEL="qwen/qwen3.8-27b"
export GROQ_VISION_MODEL="qwen/qwen3.8-27b"
```

The exact model names available to your Groq account can vary. Use a model supported by your account.

### 5. Start the API

```bash
uvicorn api:app --reload
```

The backend exposes health information at:

```text
GET /health
```

### 6. Start the frontend

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Set `VITE_API_URL` if the API is not running at the frontend's expected development URL.

## Configuration

The application reads configuration from environment variables.

### Core

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string; required |
| `GROQ_API_KEY` | Groq API credential; required for chat |
| `GROQ_MODEL` | Text model |
| `GROQ_VISION_MODEL` | Vision model |
| `CORS_ORIGINS` | Additional allowed frontend origins |
| `PUBLIC_BASE_URL` | Public backend URL, useful for deployment/OAuth |
| `LOG_LEVEL` | Application log level |

### Agent and context

| Variable | Purpose |
|---|---|
| `MAX_ITERATIONS` | Maximum agent iterations |
| `MAX_RETRIES` | Model request retry count |
| `MAX_HISTORY_MESSAGES` | Maximum history rows considered |
| `MAX_CONTEXT_HISTORY` | Conversation history bound |
| `MAX_CONTEXT_CHARS` | Normal context character budget |
| `MAX_TOOL_CONTEXT_CHARS` | Context budget when tools are active |
| `MAX_RUNTIME_CONTEXT_CHARS` | Runtime message budget |
| `MAX_TOOL_RESULT_CHARS` | Maximum retained tool-result size |
| `MAX_COMPLETION_TOKENS` | Completion-token limit |
| `JOB_MAX_RETRIES` | Background job retry count |

### Files and shell

| Variable | Purpose |
|---|---|
| `AGENT_DATA_DIR` | Application data directory |
| `AGENT_FILE_ROOT` | Sandboxed file root |
| `MAX_FILE_CHARS` | File-content limit |
| `ALLOW_SHELL` | Enable shell execution with `1`; disabled by default |
| `ALLOWED_SHELL_COMMANDS` | Comma-separated shell command allowlist |
| `SHELL_TIMEOUT` | Shell execution timeout |

### MCP

| Variable | Purpose |
|---|---|
| `MCP_SERVERS` | Environment-defined MCP servers |
| `MCP_ALLOWED_SERVERS` | Server allowlist, where applicable |
| `MCP_TIMEOUT_SECONDS` | MCP operation timeout |
| `MCP_DISCOVERY_TTL_SECONDS` | Tool-discovery cache duration |
| `MCP_HEADER_ENCRYPTION_KEY` | Key used to encrypt connector headers at rest |
| `ALLOW_LOCAL_MCP` | Explicitly allow local/private MCP targets |

### Storage and authentication

| Variable | Purpose |
|---|---|
| `R2_ENDPOINT` | Optional Cloudflare R2 endpoint |
| `R2_BUCKET` | R2 bucket |
| `R2_ACCESS_KEY_ID` | R2 access key |
| `R2_SECRET_ACCESS_KEY` | R2 secret |
| `AUTH_SESSION_DAYS` | Session lifetime |
| `AUTH_PBKDF2_ITERATIONS` | Password-hashing work factor |
| `FORCE_SECURE_COOKIES` | Secure-cookie behavior |

## MCP connectors

MCP servers can be configured through the application UI or through environment configuration.

A simple environment configuration looks like:

```bash
export MCP_SERVERS='{
  "local": {
    "transport": "stdio",
    "command": "python",
    "args": ["server.py"]
  },
  "remote": {
    "transport": "streamable-http",
    "url": "https://example.com/mcp",
    "allowed_tools": ["search"]
  }
}'
```

Remote HTTP connectors are validated to prevent private/local targets by default. Connector authentication headers can be stored encrypted when `MCP_HEADER_ENCRYPTION_KEY` is configured.

## Data and persistence

PostgreSQL is the persistent store for application state. The project uses it for:

- Chat messages and session metadata.
- Explicit and semantic memories.
- RAG/document metadata.
- Application preferences.
- MCP connector configuration.
- MCP OAuth credentials.
- The single account and authentication sessions.
- Durable background-job state and results.

Uploaded files can be kept in the configured local data directory or persisted through Cloudflare R2 when enabled.

## Authentication

The application supports exactly one account.

On first launch, the frontend can create the account. Subsequent access uses the authenticated session.

Security-related implementation includes:

- PBKDF2-SHA256 password hashing.
- HttpOnly session cookies.
- Login rate limiting.
- Protected API routes.
- Origin checks for state-changing requests.
- Security response headers.
- No multi-user data partitioning.

This architecture is intended for a private personal deployment, not a general multi-tenant service.

## File and document handling

Supported document ingestion includes common text/document formats such as:

- PDF
- DOCX
- TXT
- Markdown
- CSV
- JSON
- XML
- Supported images

Documents can be indexed into the semantic/RAG store and retrieved when relevant to a request. Scanned documents and images can use OCR fallback where supported by the parser stack.

Uploaded files are constrained to the configured file root, with path validation intended to prevent traversal outside that directory.

## Background jobs

Long-running chat requests are persisted as jobs in PostgreSQL.

The worker:

- Runs inside the existing FastAPI process.
- Executes one job at a time.
- Records progress and results in PostgreSQL.
- Retries failed jobs with bounded backoff.
- Recovers queued/retrying jobs after a process restart.

This avoids requiring Redis or a separate paid worker for the included deployment model.

A process restart can interrupt an actively executing job. Its durable job record remains in PostgreSQL and can be recovered when the service starts again.

## Testing

Run backend tests:

```bash
pytest -q
```

Run frontend tests:

```bash
cd frontend
npm test
```

Build the frontend:

```bash
cd frontend
npm run build
```

For an Android debug build:

```bash
cd frontend
npm run cap:add
npm run cap:sync
npm run android:build
```

## Deployment with Render

The repository includes `render.yaml` defining:

- A Python/FastAPI web service.
- A static React/Vite frontend.
- A PostgreSQL database.

The backend build command is:

```bash
pip install -r requirements.txt
```

The backend start command is:

```bash
uvicorn api:app --host 0.0.0.0 --port $PORT
```

The frontend is built with:

```bash
cd frontend
npm install
npm run build
```

Set the required secrets and environment-specific values in Render rather than committing credentials to the repository.

### Render limitations

The included job runner is intentionally in-process. A free web service can restart or sleep, so PostgreSQL provides durable state but does not make active execution survive a process restart.

## Security notes

This project handles credentials, uploaded files, model requests, and optional third-party connectors. Before exposing it publicly:

- Keep `GROQ_API_KEY`, database credentials, R2 credentials, MCP credentials, and encryption keys out of source control.
- Configure `MCP_HEADER_ENCRYPTION_KEY` for encrypted connector headers.
- Keep shell execution disabled unless it is actually needed.
- If shell execution is enabled, keep the command allowlist minimal.
- Prefer HTTPS for remote MCP connectors.
- Do not enable local/private MCP targets unless you control the network boundary.
- Restrict `CORS_ORIGINS` to the frontend origins you actually use.
- Review connector tool allowlists before granting access to external services.
- Back up the PostgreSQL database if conversation history and memory are important.

## License

No license file is currently assumed by this README. If you intend to publish or redistribute the project, add an explicit license to the repository.

## Project status

This repository is structured as a personal, single-user assistant application. The README intentionally documents the current architecture and setup without assuming a previous release history or migration path.
