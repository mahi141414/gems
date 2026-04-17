# Suva Gems API — Gem Server Documentation

A standalone FastAPI server exposing an **OpenAI-compatible Chat Completions API** backed by a single Gemini Gem (`fcf6f88e78ea`). Drop-in replacement for `https://api.openai.com/v1` in any OpenAI SDK or client.

Cookies are persisted to **Convex** (cloud database) so the session survives Render reloads, and auto-refreshed every 5 minutes to keep it alive indefinitely.

Base URL: `http://localhost:8001`

---

## Quick Start

### Local Development

```bash
# 1. Set API key (optional — leave empty for open access)
echo "GEM_API_KEY=sk-your-secret-key" >> .env

# 2. Export cookies from browser (Cookie Editor) into cookies.json

# 3. Start gem server (runs on port 8001 by default)
python gem_server.py

# 4. Server auto-initializes from cookies.json on startup

# 5. Chat
curl http://localhost:8001/v1/chat/completions \
  -H "Authorization: Bearer sk-your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gem",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": false
  }'
```

### Deploy to Render (with Convex)

See [Deploy to Render](#deploy-to-render) below.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GEM_API_KEY` | (falls back to `API_KEY`, then empty) | API key. Empty = no auth |
| `GEM_ID` | `fcf6f88e78ea` | Gemini Gem ID to use |
| `GEM_PORT` | `8001` | Server port |
| `CONVEX_URL` | empty | Convex deployment URL (e.g. `https://your-app.convex.cloud`). If empty, uses local `cookies.json` only |

---

## Authentication

All endpoints except `/health` require an API key when `GEM_API_KEY` is set.

Pass via any of:
- **Header:** `Authorization: Bearer sk-your-secret-key`
- **Header:** `X-API-Key: sk-your-secret-key`
- **Query param:** `?api_key=sk-your-secret-key`

If `GEM_API_KEY` is empty or unset, authentication is disabled (open access).

---

## Cookie Storage: Convex + Local

Cookies are stored in two places:

1. **Convex** (primary when `CONVEX_URL` is set) — cloud database, survives Render reloads
2. **Local `cookies.json`** (fallback) — used when Convex is unavailable or not configured

**Read flow** (on startup + auto-reinit):
```
Convex cookies:get  →  if found, write to cookies.json & use it
                        ↓ not found
                    cookies.json on disk  →  use it
                        ↓ not found
                    return None (need upload)
```

**Write flow** (on persist + upload):
```
Write to cookies.json (local)
  ↓
If CONVEX_URL set → cookies:set (Convex)
```

This means:
- Render reload → Convex has the latest auto-refreshed cookies → server reinitializes seamlessly
- Convex down → falls back to local file
- No Convex configured → works purely with local files (same as before)

---

## Endpoints

### Operational

#### `GET /health`

Self-healing health check. No auth required. Designed for uptime monitors (BetterStack, UptimeRobot, etc.).

Auto-recovers from expired sessions by re-reading cookies from Convex → local fallback.

**Response (healthy):**
```json
{
  "status": "healthy",
  "client_initialized": true,
  "account_status": "AVAILABLE",
  "cookie_source": "convex"
}
```

**Response (auto-recovered):**
```json
{
  "status": "healthy",
  "client_initialized": true,
  "account_status": "AVAILABLE",
  "auto_recovered": true,
  "previous_status": "UNAUTHENTICATED"
}
```

**Response (down — cookies expired):**
```json
{
  "status": "down",
  "client_initialized": false,
  "message": "Auto-reinit failed. Upload cookies via /cookies/update"
}
```

---

#### `POST /cookies/update`

Upload a new `cookies.json` file. Saves to **both** Convex and local. Server auto-reinitializes.

**Body (multipart/form-data):**
| Field | Type | Description |
|-------|------|-------------|
| `file` | file | JSON file (Cookie Editor export) |

**Response:**
```json
{
  "status": "cookies_updated",
  "message": "Cookies applied and client reinitialized",
  "persisted_to": "convex+local"
}
```

**Example:**
```bash
curl -H "Authorization: Bearer sk-key" \
  -F "file=@cookies.json" \
  http://localhost:8001/cookies/update
```

---

#### `POST /reinit`

Force re-initialize client. Reads cookies from Convex → local fallback. Clears all sessions.

---

### Models

#### `GET /v1/models`

OpenAI-compatible model listing.

**Response:**
```json
{
  "object": "list",
  "data": [
    {
      "id": "gemini-2.0-flash",
      "object": "model",
      "created": 1713222000,
      "owned_by": "google",
      "permission": [],
      "root": "gemini-2.0-flash"
    }
  ]
}
```

---

### Sessions

Sessions are persistent Gemini chat conversations tied to the Gem. Create a session, then reference it across multiple chat completion requests to maintain conversation context.

#### `POST /v1/sessions`

Create a new persistent chat session with the Gem.

**Response:**
```json
{
  "id": "a1b2c3d4-...",
  "object": "session",
  "created_at": 1713222000,
  "gemini_chat_id": "",
  "gem_id": "fcf6f88e78ea"
}
```

Save the `id` — pass it as `session_id` in chat completion requests to continue the conversation.

---

#### `GET /v1/sessions`

List all active sessions.

**Response:**
```json
{
  "object": "list",
  "data": [
    {
      "id": "a1b2c3d4-...",
      "object": "session",
      "created_at": 1713222000,
      "gemini_chat_id": "c_xxxxx",
      "gem_id": "fcf6f88e78ea"
    }
  ]
}
```

---

#### `GET /v1/sessions/{session_id}`

Get session info **including full message history** from Gemini.

**Response:**
```json
{
  "id": "a1b2c3d4-...",
  "object": "session",
  "created_at": 1713222000,
  "gemini_chat_id": "c_xxxxx",
  "gem_id": "fcf6f88e78ea",
  "messages": [
    {"role": "user", "content": "Hello!"},
    {"role": "model", "content": "Hi there! How can I help?"}
  ]
}
```

---

#### `DELETE /v1/sessions/{session_id}`

Delete a session (removes from server memory; does not delete from Gemini history).

---

### Chat Completions

#### `POST /v1/chat/completions`

OpenAI-compatible chat completion endpoint. All requests go through the configured Gem.

**Request body:**
| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `model` | string | `"gem"` | Ignored — always uses the configured Gem |
| `messages` | array | required | Chat messages. Only the **last user message** is sent to Gemini |
| `temperature` | float | null | Ignored (Gemini doesn't expose this per-request) |
| `max_tokens` | int | null | Ignored |
| `stream` | bool | false | Enable SSE streaming |
| `session_id` | string | null | Persistent session ID (from `POST /v1/sessions`). If omitted, a new session is auto-created |

**Non-streaming response:**
```json
{
  "id": "chatcmpl-abc123...",
  "object": "chat.completion",
  "created": 1713222000,
  "model": "fcf6f88e78ea",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! How can I help you today?"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
  },
  "session_id": "a1b2c3d4-...",
  "gemini_chat_id": "c_xxxxx"
}
```

If Gemini returns multiple candidates, they appear as additional choices.

**Streaming response (SSE):**
```
data: {"id":"chatcmpl-abc123...","object":"chat.completion.chunk","created":1713222000,"model":"fcf6f88e78ea","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {"id":"chatcmpl-abc123...","object":"chat.completion.chunk","created":1713222000,"model":"fcf6f88e78ea","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc123...","object":"chat.completion.chunk","created":1713222000,"model":"fcf6f88e78ea","choices":[{"index":0,"delta":{"content":"!"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc123...","object":"chat.completion.chunk","created":1713222000,"model":"fcf6f88e78ea","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

**Error during stream:**
```
data: {"error":{"message":"Rate limit exceeded","type":"rate_limit","code":"429"}}

data: [DONE]
```

---

## Usage Examples

### Single-shot (no session)

Each call creates a new temporary session. No context is carried over.

```bash
curl http://localhost:8001/v1/chat/completions \
  -H "Authorization: Bearer sk-key" \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "What is 2+2?"}],
    "stream": false
  }'
```

### Persistent session (multi-turn)

Create a session, then reference it to maintain conversation context:

```bash
# 1. Create session
SESSION=$(curl -s http://localhost:8001/v1/sessions \
  -H "Authorization: Bearer sk-key" | python -c "import sys,json; print(json.load(sys.stdin)['id'])")

# 2. First message
curl http://localhost:8001/v1/chat/completions \
  -H "Authorization: Bearer sk-key" \
  -H "Content-Type: application/json" \
  -d "{
    \"session_id\": \"$SESSION\",
    \"messages\": [{\"role\": \"user\", \"content\": \"My name is Alice\"}],
    \"stream\": false
  }"

# 3. Follow-up (Gem remembers "Alice")
curl http://localhost:8001/v1/chat/completions \
  -H "Authorization: Bearer sk-key" \
  -H "Content-Type: application/json" \
  -d "{
    \"session_id\": \"$SESSION\",
    \"messages\": [{\"role\": \"user\", \"content\": \"What is my name?\"}],
    \"stream\": false
  }"

# 4. View full history
curl http://localhost:8001/v1/sessions/$SESSION \
  -H "Authorization: Bearer sk-key"
```

### Streaming with Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8001/v1",
    api_key="sk-your-secret-key",
)

# Non-streaming
response = client.chat.completions.create(
    model="gem",
    messages=[{"role": "user", "content": "Explain quantum computing"}],
)
print(response.choices[0].message.content)

# Streaming
for chunk in client.chat.completions.create(
    model="gem",
    messages=[{"role": "user", "content": "Explain quantum computing"}],
    stream=True,
):
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

### Streaming with JavaScript

```javascript
const response = await fetch("http://localhost:8001/v1/chat/completions", {
  method: "POST",
  headers: {
    "Authorization": "Bearer sk-your-secret-key",
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    model: "gem",
    messages: [{ role: "user", content: "Explain quantum computing" }],
    stream: true,
  }),
});

const reader = response.body.getReader();
const decoder = new TextDecoder();

while (true) {
  const { done, value } = await reader.read();
  if (done) break;
  const chunk = decoder.decode(value);
  for (const line of chunk.split("\n")) {
    if (line.startsWith("data: ") && line !== "data: [DONE]") {
      const data = JSON.parse(line.slice(6));
      const content = data.choices?.[0]?.delta?.content;
      if (content) process.stdout.write(content);
    }
  }
}
```

### Session with extra header

Pass `session_id` as a custom field in the request body:

```bash
curl http://localhost:8001/v1/chat/completions \
  -H "Authorization: Bearer sk-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gem",
    "session_id": "a1b2c3d4-...",
    "messages": [{"role": "user", "content": "Continue our chat"}],
    "stream": true
  }'
```

---

## Session Lifecycle

```
POST /v1/sessions          →  Create session → {"id": "sid-xxx", ...}
                                     │
POST /v1/chat/completions  ←  Send messages with "session_id": "sid-xxx"
  (repeat)                           │
GET  /v1/sessions/sid-xxx  →  Load full message history
                                     │
DELETE /v1/sessions/sid-xxx →  Remove session from memory
```

When `session_id` is omitted from `/v1/chat/completions`, a new session is auto-created and its `session_id` is returned in the response. You can reuse this ID for follow-up messages.

---

## Error Codes

| HTTP Status | Meaning |
|-------------|---------|
| `400` | Bad request — no user message, invalid model |
| `401` | Auth failure — invalid API key or expired Gemini cookies |
| `403` | Temporarily blocked by Google |
| `404` | Session not found |
| `429` | Usage limit exceeded (Google rate limit) |
| `500` | Server error |
| `502` | Gemini API error |
| `503` | Client not initialized |
| `504` | Request timeout |

---

## Cookie Management & Session Persistence

The server is **self-sustaining** after an initial cookie upload:

1. **`auto_refresh`** rotates `__Secure-1PSIDTS` every ~9 minutes → session stays valid
2. **`persist_cookies`** saves the rotated value to both Convex and `cookies.json` every 5 minutes → survives Render reloads
3. **`/health`** auto-recovers from expired sessions on each monitor ping (reads from Convex first)
4. **Startup** reads cookies from Convex → local fallback → auto-initializes

You only need to export cookies **once**. After that, the system maintains itself indefinitely. Only re-export if BetterStack alerts you that the session is `down` (cookies truly expired, which takes months).

### Remote cookie update (no SSH needed)

```bash
curl -H "Authorization: Bearer sk-key" \
  -F "file=@cookies.json" \
  http://your-server:8001/cookies/update
```

Server auto-reinitializes with the new cookies — no restart needed. Cookies are saved to both Convex and local file.

---

## Deploy to Render

### 1. Set up Convex

```bash
# Install Convex CLI
npm install -g convex

# Create a new Convex project (or use existing)
cd suva-gems
npx convex dev

# This will:
# - Create a Convex project if needed
# - Deploy the functions from convex/ directory
# - Print your CONVEX_URL (e.g. https://happy-cat-123.convex.cloud)
```

The `convex/` directory in this repo contains:
- `schema.ts` — defines the `cookies` table
- `cookies.ts` — `get` query and `set` mutation

### 2. Push to GitHub

Push the repo contents to a GitHub repository. **Don't commit `.env` or `cookies.json`** — cookies are stored in Convex.

### 3. Deploy on Render

Create a new **Web Service** on Render connected to your GitHub repo, or use the included `render.yaml`:

```yaml
services:
  - type: web
    name: suva-gems-api
    runtime: python
    buildCommand: pip install -r requirements.txt
    startCommand: python gem_server.py
    envVars:
      - key: GEM_API_KEY
        sync: false
      - key: CONVEX_URL
        sync: false
      - key: GEM_ID
        value: fcf6f88e78ea
      - key: GEM_PORT
        value: "8001"
```

Set these environment variables on Render:
- `GEM_API_KEY` — your API key
- `CONVEX_URL` — your Convex deployment URL from step 1

### 4. Upload cookies

```bash
curl -H "Authorization: Bearer your-key" \
  -F "file=@cookies.json" \
  https://your-render-app.onrender.com/cookies/update
```

This uploads cookies to both Convex and the server. Done — you won't need to do this again.

### 5. Set up monitoring

Point BetterStack/UptimeRobot to `GET https://your-render-app.onrender.com/health` every 5 minutes.

---

## Running Locally Alongside the Main Server

The gem server runs on **port 8001** by default, separate from the main API on port 8000.

```bash
# Terminal 1: Main API
python server.py          # port 8000

# Terminal 2: Gem endpoint
python gem_server.py      # port 8001
```

Both share the same `cookies.json` locally. With Convex configured, the gem server prefers Convex as the cookie source.

---

## Uptime Monitoring

Point your monitor (BetterStack, UptimeRobot, etc.) to `GET /health` every 5 minutes. If the session dies, `/health` auto-recovers by reading cookies from Convex → local fallback. If cookies themselves expired, it returns `down` and the monitor alerts you.
