# Gemini API Server - Developer Guide

Production-ready REST API wrapper for Google Gemini. All endpoints, examples, deployment info, and cookie management are in one place.

## 🚀 Quick Setup (5 minutes)

### Prerequisites
- Python 3.9+ installed
- Google Gemini account

### 2. Get Your Cookies
1. Visit https://gemini.google.com and login
2. Right-click → Inspect (F12) → Storage → Cookies
3. Copy these two cookies:
   - `__Secure-1PSID`
   - `__Secure-1PSIDTS`

### 3. Setup
```bash
# Create virtual environment
python -m venv venv

# Activate (Windows)
venv\Scripts\activate
# Or (Mac/Linux)
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create cookies.json with your cookies
@'
{
  "__Secure-1PSID": "your_psid_here",
  "__Secure-1PSIDTS": "your_psidts_here"
}
'@ | Set-Content cookies.json
```

### 4. Run Server
```bash
python main.py
```

Server runs on `http://localhost:8000`

**Test it:**
- API Dashboard: http://localhost:8000
- Cookie Admin: http://localhost:8000/admin
- API Docs: http://localhost:8000/docs

### Admin Page

Set this environment variable on the server:

```bash
ADMIN_PASSWORD=your-strong-password
```

Then open `http://localhost:8000/admin` to view and update `cookies.json`.

---

## 📡 API Endpoints Reference

### Health Check
```bash
GET /health
```
Response: `{"status": "healthy", "client_initialized": true}`

### List Models
```bash
GET /models
```
Response: List of available Gemini models

---

## 💬 Text Generation

### Simple Text Generation
```bash
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is AI?"}'
```

**Request:**
```json
{
  "prompt": "Your question here",
  "model": "gemini-2.0-flash",     // optional
  "stream": false,                  // optional
  "temporary": false,               // optional
  "timeout": 60                     // optional (seconds)
}
```

**Response:**
```json
{
  "text": "AI is...",
  "images": [],
  "videos": [],
  "model": "gemini-2.0-flash"
}
```

### Streaming Responses
```json
{
  "prompt": "Write a story",
  "stream": true
}
```
Returns Server-Sent Events with text chunks

### Generate Images
```bash
curl -X POST http://localhost:8000/generate-image \
  -H "Content-Type: application/json" \
  -d '{"prompt": "A cat in a jungle"}'
```

### Upload & Analyze Files
```bash
curl -X POST "http://localhost:8000/upload-files?prompt=Analyze%20this" \
  -F "files=@image.jpg" \
  -F "files=@document.pdf"
```

---

## 🗣️ Chat & Conversations

### Start Chat
```bash
curl -X POST http://localhost:8000/chat/start \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Hello!"}'
```

**Response:**
```json
{
  "session_id": "c_abc123...",
  "text": "Hi! How can I help?",
  "model": "gemini-2.0-flash"
}
```
**Save the `session_id` for continuing the conversation**

### Continue Chat
```bash
curl -X POST http://localhost:8000/chat/reply \
  -H "Content-Type: application/json" \
  -d '{"session_id": "c_abc123...", "prompt": "Tell me more"}'
```

### Get Chat History
```bash
curl http://localhost:8000/chat/history/c_abc123...
```

### List All Chats
```bash
curl http://localhost:8000/chat/list
```

### Delete Chat
```bash
curl -X DELETE http://localhost:8000/chat/c_abc123...
```

---

## 🎭 Custom Gems (AI Personalities)

### List Gems
```bash
curl http://localhost:8000/gems
```

### Create Gem
```bash
curl -X POST http://localhost:8000/gems/create \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Python Expert",
    "prompt": "You are an expert Python programmer",
    "description": "Helps with Python questions"
  }'
```

### Delete Gem
```bash
curl -X DELETE http://localhost:8000/gems/gem_id_here
```

### Use Gem in Chat
```bash
curl -X POST http://localhost:8000/chat/start \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Hello!",
    "gem": "gem_id_here"
  }'
```

---

## 🔍 Deep Research

```bash
curl -X POST http://localhost:8000/deep-research \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Latest AI developments",
    "timeout": 300
  }'
```

**Note:** This blocks until research completes (up to 5 minutes)

---

## 🐍 Python Examples

### Simple Generation
```python
import httpx
import asyncio

async def generate(prompt):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            'http://localhost:8000/generate',
            json={'prompt': prompt}
        )
        return response.json()['text']

# Use it
result = asyncio.run(generate('What is Python?'))
print(result)
```

### Chat Conversation
```python
import httpx
import asyncio

async def chat_example():
    async with httpx.AsyncClient() as client:
        # Start chat
        response = await client.post(
            'http://localhost:8000/chat/start',
            json={'prompt': 'Hello!'}
        )
        chat_data = response.json()
        session_id = chat_data['session_id']
        print(f"Bot: {chat_data['text']}")
        
        # Continue chat
        response = await client.post(
            'http://localhost:8000/chat/reply',
            json={
                'session_id': session_id,
                'prompt': 'Tell me about AI'
            }
        )
        print(f"Bot: {response.json()['text']}")

asyncio.run(chat_example())
```

### Stream Responses
```python
import httpx
import asyncio
import json

async def stream_example():
    async with httpx.AsyncClient() as client:
        async with client.stream(
            'POST',
            'http://localhost:8000/generate',
            json={'prompt': 'Write a story', 'stream': True}
        ) as response:
            async for line in response.aiter_lines():
                if line.startswith('data: '):
                    chunk = json.loads(line[6:])
                    print(chunk['delta'], end='', flush=True)

asyncio.run(stream_example())
```

---

## 🌐 JavaScript Examples

### Simple Generation
```javascript
async function generate(prompt) {
  const response = await fetch('http://localhost:8000/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt })
  });
  const data = await response.json();
  return data.text;
}

// Use it
generate('What is JavaScript?').then(console.log);
```

### Chat Conversation
```javascript
async function chat() {
  // Start chat
  let response = await fetch('http://localhost:8000/chat/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt: 'Hello!' })
  });
  let data = await response.json();
  const sessionId = data.session_id;
  console.log('Bot:', data.text);
  
  // Continue chat
  response = await fetch('http://localhost:8000/chat/reply', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: sessionId,
      prompt: 'Tell me about AI'
    })
  });
  data = await response.json();
  console.log('Bot:', data.text);
}

chat();
```

### Stream Responses
```javascript
async function generateStream(prompt) {
  const response = await fetch('http://localhost:8000/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt, stream: true })
  });

  const reader = response.body.getReader();
  const decoder = new TextDecoder();

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    const text = decoder.decode(value);
    const lines = text.split('\n');
    
    for (const line of lines) {
      if (line.startsWith('data: ')) {
        try {
          const json = JSON.parse(line.slice(6));
          process.stdout.write(json.delta);
        } catch (e) {}
      }
    }
  }
}

generateStream('Write a story');
```

---

## ⚛️ React Component Example

```jsx
import { useState } from 'react';

export default function GeminiChat() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [sessionId, setSessionId] = useState(null);
  const [loading, setLoading] = useState(false);

  const sendMessage = async () => {
    if (!input || loading) return;
    setLoading(true);

    try {
      if (!sessionId) {
        // Start new chat
        const res = await fetch('http://localhost:8000/chat/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prompt: input })
        });
        const data = await res.json();
        setSessionId(data.session_id);
        setMessages([
          { role: 'user', text: input },
          { role: 'assistant', text: data.text }
        ]);
      } else {
        // Continue chat
        const res = await fetch('http://localhost:8000/chat/reply', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_id: sessionId,
            prompt: input
          })
        });
        const data = await res.json();
        setMessages(prev => [
          ...prev,
          { role: 'user', text: input },
          { role: 'assistant', text: data.text }
        ]);
      }
    } catch (error) {
      console.error('Error:', error);
    }

    setInput('');
    setLoading(false);
  };

  return (
    <div style={{ maxWidth: '600px', margin: '0 auto', padding: '20px' }}>
      <div style={{ 
        border: '1px solid #ccc', 
        height: '400px', 
        overflow: 'auto',
        marginBottom: '20px',
        padding: '10px',
        borderRadius: '5px'
      }}>
        {messages.map((msg, i) => (
          <div key={i} style={{ marginBottom: '10px' }}>
            <strong>{msg.role === 'user' ? 'You' : 'Bot'}:</strong> {msg.text}
          </div>
        ))}
      </div>
      <input
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyPress={(e) => e.key === 'Enter' && sendMessage()}
        disabled={loading}
        placeholder="Type message..."
        style={{ width: '80%', padding: '10px' }}
      />
      <button 
        onClick={sendMessage} 
        disabled={loading}
        style={{ width: '18%', padding: '10px', marginLeft: '2%' }}
      >
        Send
      </button>
    </div>
  );
}
```

---

## 🎯 NextJS Integration

### API Route: `pages/api/gemini/[...path].ts`

```typescript
import { NextApiRequest, NextApiResponse } from 'next';

const GEMINI_API = process.env.NEXT_PUBLIC_GEMINI_API || 'http://localhost:8000';

export default async function handler(
  req: NextApiRequest,
  res: NextApiResponse
) {
  const { path } = req.query;
  const pathStr = Array.isArray(path) ? path.join('/') : path;

  try {
    const response = await fetch(`${GEMINI_API}/${pathStr}`, {
      method: req.method,
      headers: {
        'Content-Type': 'application/json',
        ...req.headers,
      },
      body: req.body ? JSON.stringify(req.body) : undefined,
    });

    const data = await response.json();
    res.status(response.status).json(data);
  } catch (error) {
    res.status(500).json({ error: 'API Error' });
  }
}
```

### NextJS Component: `components/ChatWidget.tsx`

```typescript
'use client';

import { useState } from 'react';

export default function ChatWidget() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [sessionId, setSessionId] = useState<string | null>(null);

  const send = async () => {
    if (!input.trim()) return;

    try {
      const endpoint = sessionId ? '/api/gemini/chat/reply' : '/api/gemini/chat/start';
      const body = sessionId
        ? { session_id: sessionId, prompt: input }
        : { prompt: input };

      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });

      const data = await res.json();

      if (!sessionId && data.session_id) {
        setSessionId(data.session_id);
      }

      setMessages(prev => [
        ...prev,
        { role: 'user', text: input },
        { role: 'assistant', text: data.text }
      ]);

      setInput('');
    } catch (error) {
      console.error(error);
    }
  };

  return (
    <div className="chat-widget">
      <div className="messages">
        {messages.map((msg, i) => (
          <div key={i} className={`message ${msg.role}`}>
            {msg.text}
          </div>
        ))}
      </div>
      <input
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyPress={(e) => e.key === 'Enter' && send()}
        placeholder="Type message..."
      />
      <button onClick={send}>Send</button>
    </div>
  );
}
```

---

## 🔧 Configuration

### Environment Variables (`.env`)
```
ADMIN_PASSWORD=your_strong_admin_password
API_HOST=0.0.0.0
API_PORT=8000
DEBUG=false
```

### cookies.json
This file is the only source for Gemini cookies:
```json
{
  "__Secure-1PSID": "your_cookie_here",
  "__Secure-1PSIDTS": "your_cookie_here"
}
```

### All Available Endpoints

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | /health | Health check |
| GET | /models | List models |
| POST | /generate | Single-turn generation |
| POST | /generate-image | Generate images |
| POST | /upload-files | Analyze files |
| POST | /chat/start | Start conversation |
| POST | /chat/reply | Continue chat |
| GET | /chat/history/{id} | Get chat history |
| GET | /chat/list | List all chats |
| DELETE | /chat/{id} | Delete chat |
| POST | /chat/candidates/{id} | Get alternatives |
| POST | /chat/choose-candidate/{id} | Choose alternative |
| GET | /gems | List gems |
| POST | /gems/create | Create gem |
| DELETE | /gems/{id} | Delete gem |
| POST | /deep-research | Deep research |
| POST | /logging/set-level | Set log level |

---

## ⚠️ Common Issues & Fixes

### "Credentials not configured"
```bash
# Make sure .env file exists with:
GEMINI_PSID=your_value
GEMINI_PSIDTS=your_value

# Restart server: python main.py
```

### "Session not found"
- Use a valid session ID from `/chat/start`
- Sessions don't persist across server restarts

### "403 Unauthorized"
- Your cookies expired
- Get fresh ones from https://gemini.google.com (in incognito window)
- Update `.env` file
- Restart server

### "Connection refused"
- Server not running - run: `python main.py`
- Using wrong port - default is 8000
- Port in use - change API_PORT in .env

---

## 🚀 Deployment

### Local Development
```bash
python main.py
```

### PM2 (Production)
```bash
npm install -g pm2
pm2 start main.py --name gemini-api
pm2 save
```

### Docker
```bash
docker build -t gemini-api .
docker run -p 8000:8000 \
  -e GEMINI_PSID=your_value \
  -e GEMINI_PSIDTS=your_value \
  gemini-api
```

### Docker Compose
```bash
docker-compose up -d
```

---

## 📊 Response Format

All successful responses follow this pattern:

```json
{
  "text": "Main response",
  "images": [
    {
      "url": "https://...",
      "title": "Image title",
      "alt": "Alt text"
    }
  ],
  "videos": [],
  "model": "gemini-2.0-flash"
}
```

Error responses:
```json
{
  "detail": "Error description"
}
```

---

## 🆘 Support

- **Interactive API Docs**: Visit `http://localhost:8000/docs` when server is running
- **Web Dashboard**: Test endpoints at `http://localhost:8000`
- **GitHub**: https://github.com/HanaokaYuzu/Gemini-API

---

## 📝 License

AGPL-3.0 License (from gemini-webapi)

---

**Ready to integrate!** 🚀 Start at "Quick Setup" above if you haven't already.
