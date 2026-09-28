# Personal AI Assistant

A single-user personal AI assistant built with Python, FastAPI, React/Vite, Groq, persistent chat history, semantic memory, RAG, local file tools, multimodal attachments, and MCP connectors.

This repository is intentionally designed for one personal instance, not as a multi-user SaaS application. It has exactly one account with a login screen and persistent session so the personal instance can be protected without introducing multi-user data partitioning.

## Features

### Conversational agent
- Groq-powered chat with configurable model and vision model.
- Bounded conversation context.
- Retry handling for transient model failures.
- Tool-aware agent loop with execution limits and recovery.
- Streaming responses through Server-Sent Events for interactive requests.
- Durable chat job state in PostgreSQL with a lightweight in-process runner; the browser can disconnect without cancelling an active task.
- Regenerate the latest answer.
- Edit and resend a user message.
- Multiple local chat sessions with titles, rename, delete, and clear-history controls.

### Memory and RAG
- Persistent explicit memories.
- Semantic memory retrieval with FastEmbed.
- Keyword fallback retrieval.
- Save, list, search, and delete memories.
- Custom instructions and response-style preferences.
- Document chunking and semantic retrieval.
- Source-scoped RAG for attachments.
- PDF, DOCX, TXT, Markdown, CSV, JSON, XML and common image parsing.
- OCR fallback for scanned PDFs and images.
- Re-indexing a source replaces its previous chunks.
- RAG source status reporting.

### Files and multimodal input
- Upload, list, download and delete files.
- 10 MB upload limit.
- Path traversal protection.
- Optional Cloudflare R2 persistence.
- Image attachments sent to the configured vision model.
- Automatic document indexing after upload.

### Built-in tools
- Safe arithmetic calculator without eval().
- Web search through DDGS.
- Sandboxed text-file read/write.
- Optional allowlisted shell execution.
- shell=False, timeout and working-directory restrictions.

### MCP ecosystem
- Environment-defined MCP servers.
- Persistent MCP connectors for this single assistant instance.
- Streamable HTTP and SSE transports.
- Private/local target protection for HTTP connectors.
- Backend-only authentication headers.
- Paginated MCP tool discovery.
- Tool-schema generation and relevance filtering.
- Per-connector tool allowlists.
- Discovery caching and bounded timeouts.
- Connector diagnostics.
- OAuth support for Streamable HTTP connectors.
- OAuth tokens and client details persisted without user identifiers.

### Preferences
- System / Light / Dark appearance.
- Language and haptics preferences.
- Web-search and memory toggles.
- Custom instructions.
- Natural / Concise / Detailed response style.
- Persistent update and reset logic.

### Frontend
- React/Vite responsive conversational UI.
- Conversation sidebar and search.
- New chat, rename, delete and clear-history controls.
- Streaming responses.
- Regenerate and edit/resend.
- Camera, photo and file attachment menu.
- File manager with search/download/delete.
- Memory manager.
- Settings for appearance, AI behavior, customization, connectors, privacy and data controls.
- Mobile-responsive layout.
- Single-account login and first-run account setup screen.
- Account management for name/email, password change and sign out.

## Architecture

Interactive requests can still use the normal FastAPI/SSE path. Long-running chat work uses a free-tier job path:

~~~
React / Vite
     |
     | POST job (fast 202 response)
     v
FastAPI Web Service
     |
     +--> PostgreSQL (durable job state/results)
     |
     +--> In-process job runner (1 concurrent job)
              |
              v
          Agent runtime
          |      |       |
          |      |       +--> Groq / vision model
          |      |
          |      +----------> Memory + RAG
          |
          +-----------------> Local tools + MCP
~~~

The PostgreSQL row is the durable source of truth. On process startup, queued/retrying jobs are recovered and submitted again. The runner uses the same Render Free web-service process, so no paid Background Worker or Redis/Key Value broker is required.

**Free-tier limitation:** Render Free web services can restart and can spin down after 15 minutes without inbound traffic. Therefore the PostgreSQL job record is durable, but execution is not guaranteed through a platform restart/spindown.

## Single-user data model

Persistent state is global to this assistant instance:

- messages(session_id, ...)
- chat_metadata(session_id, ...)
- memories(...)
- semantic_memories(...)
- rag_documents(...)
- preferences(id=1, ...)
- mcp_connectors(...)
- mcp_oauth_credentials(connector_id, ...)

Legacy user_id columns are removed by initialization/migration paths where applicable. Authentication tables are stored in PostgreSQL alongside application state.

## Project structure

- agent.py — agent runtime, context assembly, retries, tools and streaming.
- orchestration.py — task classification, execution planning and tool-result validation.
- tools.py — calculator, web search, file tools, chat history and basic memory.
- memory.py — semantic memory and RAG.
- document_parser.py — document extraction and OCR routing.
- multimodal.py — uploads, local file access and optional R2.
- api.py — FastAPI REST/SSE API and durable job endpoints.
- jobs.py — PostgreSQL-backed job state and results.
- task_queue.py — free in-process job executor and pending-job recovery.
- tasks.py — background chat execution and bounded retry policy.
- connectors.py — MCP connector storage and validation.
- mcp_registry.py — environment and connector MCP registry.
- mcp_client.py — MCP discovery and execution.
- mcp_oauth.py — persistent MCP OAuth state and callbacks.
- preferences.py — single-instance preferences.
- frontend/ — React/Vite application.
- tests/ — backend and frontend tests.

## Setup

~~~
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GROQ_API_KEY="your-key"
~~~

Run the API:

~~~
uvicorn api:app --reload
~~~

Run the frontend:

~~~
cd frontend
npm install
npm run dev
~~~

Run tests:

~~~
pytest -q
cd frontend && npm test && npm run build
~~~

## Important configuration

GROQ_API_KEY, GROQ_MODEL, GROQ_VISION_MODEL, MAX_ITERATIONS, MAX_RETRIES, MAX_HISTORY_MESSAGES, MAX_CONTEXT_HISTORY, MAX_CONTEXT_CHARS, AGENT_DATA_DIR, AGENT_FILE_ROOT, DATABASE_URL, JOB_MAX_RETRIES, ALLOW_SHELL, ALLOWED_SHELL_COMMANDS, SHELL_TIMEOUT, CORS_ORIGINS, MCP_SERVERS, MCP_ALLOWED_SERVERS, MCP_TIMEOUT_SECONDS, MCP_DISCOVERY_TTL_SECONDS, PUBLIC_BASE_URL, R2_ENDPOINT, R2_BUCKET, R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY are supported.

## MCP example

~~~
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
~~~

MCP discovery reads all available tool pages and filters them through configured allowlists before exposing schemas to the model.

## Authentication and persistence

- Exactly one account can be configured. A second account cannot be created.
- Passwords are stored as PBKDF2-SHA256 password hashes; raw passwords are never stored.
- Authentication uses an HttpOnly session cookie backed by the persistent database.
- All application API routes are protected after login.
- DATABASE_URL is mandatory; the application has no SQLite fallback.
- Background jobs are persisted in PostgreSQL and executed by a single in-process runner on the existing web service.
- R2 is optional for file persistence; local attachments are available to jobs while the same service process remains alive.
- A process restart/spindown can interrupt execution; the PostgreSQL job row remains and is recovered on the next service startup/request.

## Deployment

Render configuration uses only the Free-compatible FastAPI web service, React/Vite static frontend and PostgreSQL. No paid Background Worker or paid Key Value queue is required. Render's Free web service can spin down after 15 minutes without inbound traffic, so this architecture provides durable job state rather than guaranteed always-on execution.

## Security boundaries

- File paths are restricted to the configured file root.
- Shell execution is disabled by default.
- Shell commands are allowlisted and executed without a shell.
- MCP HTTP connectors reject private/local targets by default.
- Connector credentials and OAuth tokens remain backend-side.
- MCP tool access is restricted by explicit allowlists.
- Chat requests have a process-local rate limit.
- Security response headers are enabled.

This project is intended for a personal deployment where the operator controls the backend, database and connected services.
