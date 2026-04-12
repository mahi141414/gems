"""
Gemini API Server - FastAPI wrapper for Google Gemini web app
"""

from fastapi import FastAPI, HTTPException, Query, File, UploadFile, Header, Request
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import asyncio
import json
import os
import hmac
from pathlib import Path
import tempfile
from dotenv import load_dotenv
import httpx

from gemini_webapi import GeminiClient

load_dotenv()

# Initialize FastAPI app
app = FastAPI(
    title="Gemini API Server",
    description="FastAPI wrapper for Google Gemini web app with streaming support",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global client instance
gemini_client: Optional[GeminiClient] = None
client_init_lock = asyncio.Lock()  # Prevent race conditions on first init
client_ready = False  # Track if client is ready
generation_lock = asyncio.Semaphore(1)  # Shared Gemini client is not safe for parallel generates
ADMIN_PASSWORD_ENV = "ADMIN_PASSWORD"
API_KEY_ENV = "API_KEY"
COOKIE_FILE = Path("cookies.json")

# Image storage directory for temporary images (5-minute TTL)
IMAGE_STORAGE_DIR = Path("temp_images")
IMAGE_STORAGE_DIR.mkdir(exist_ok=True)
IMAGE_TTL_SECONDS = 300  # 5 minutes

# === Request/Response Models ===

class ContentRequest(BaseModel):
    """Request model for single-turn content generation"""
    prompt: str
    model: Optional[str] = None
    temporary: bool = False
    stream: bool = False


class ChatMessage(BaseModel):
    """Single chat message"""
    role: str
    content: str


class ChatSessionRequest(BaseModel):
    """Request model for starting a chat session"""
    prompt: str
    model: Optional[str] = None
    gem: Optional[str] = None
    temporary: bool = False


class ChatReplyRequest(BaseModel):
    """Request model for replying in a chat session"""
    session_id: str
    prompt: str
    temporary: bool = False


class GenerateImageRequest(BaseModel):
    """Request model for image generation"""
    prompt: str
    model: Optional[str] = None


class DeepResearchRequest(BaseModel):
    """Request model for deep research"""
    query: str
    poll_interval: float = 10.0
    timeout: float = 600.0


class GemCreateRequest(BaseModel):
    """Request model for creating a custom gem"""
    name: str
    prompt: str
    description: Optional[str] = None


class ModelInfo(BaseModel):
    """Model information"""
    model_name: str
    display_name: str


class CookieUpdateRequest(BaseModel):
    psid: str
    psidts: str = ""


# === Helper Functions ===

async def init_client(psid: str, psidts: str = ""):
    """Initialize Gemini client - OPTIMIZED FOR SPEED"""
    global gemini_client, client_ready
    try:
        print(f"🔐 Initializing Gemini client with PSID: {psid[:20]}...")
        gemini_client = GeminiClient(psid, psidts, proxy=None)
        print("⏳ Awaiting client initialization...")
        
        # Increase timeout significantly for image generation
        await gemini_client.init(
            timeout=120,       # Increased from 30 to 120s to prevent watchdog timeouts druing image generation
            auto_close=False,  # Keep client alive for fast responses
            close_delay=0,     
            auto_refresh=True
        )
        client_ready = True
        print("✅ Gemini client successfully initialized!")
        return True
    except Exception as e:
        print(f"❌ Error initializing client: {e}")
        import traceback
        traceback.print_exc()
        client_ready = False
        return False


async def ensure_client():
    """Ensure client is initialized - OPTIMIZED WITH LOCKING"""
    global gemini_client, client_ready, client_init_lock
    
    # If client is ready, return immediately
    if gemini_client is not None and client_ready:
        return
    
    # Acquire lock to prevent multiple simultaneous initializations
    async with client_init_lock:
        # Double-check after acquiring lock (another coroutine might have initialized)
        if gemini_client is not None and client_ready:
            return
        
        psid = None
        psidts = ""

        if COOKIE_FILE.exists():
            print("📄 Loading cookies from cookies.json...")
            with COOKIE_FILE.open("r", encoding="utf-8") as f:
                cookies = json.load(f)
                psid = cookies.get("__Secure-1PSID")
                psidts = cookies.get("__Secure-1PSIDTS", "")
                print(f"   Found PSID: {psid[:20]}..." if psid else "   PSID not found!")
        else:
            print("❌ cookies.json not found")
        
        if not psid:
            raise HTTPException(
                status_code=401,
                detail="Gemini credentials not configured. Create cookies.json with your Gemini cookies"
            )
        
        if not await init_client(psid, psidts):
            raise HTTPException(status_code=500, detail="Failed to initialize Gemini client")


def generation_kwargs(model: Optional[str], temporary: bool) -> dict:
    kwargs = {'temporary': temporary}
    if model:
        kwargs['model'] = model
    return kwargs


def load_admin_password() -> str:
    return os.getenv(ADMIN_PASSWORD_ENV, "")


def load_api_key() -> str:
    return os.getenv(API_KEY_ENV, "").strip()


def verify_admin_password(password: str):
    admin_password = load_admin_password()
    if not admin_password or not hmac.compare_digest(password, admin_password):
        raise HTTPException(status_code=401, detail="Invalid admin password")


def verify_api_key(request: Request):
    expected_api_key = load_api_key()
    if not expected_api_key:
        raise HTTPException(
            status_code=500,
            detail="Server misconfigured: API_KEY is not set"
        )

    provided_api_key = (
        request.headers.get("x-api-key")
        or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    ).strip()

    if not provided_api_key and request.url.path == "/admin.html":
        provided_api_key = request.query_params.get("api_key", "").strip()

    if not hmac.compare_digest(provided_api_key, expected_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def read_cookie_file() -> dict:
    if not COOKIE_FILE.exists():
        return {"__Secure-1PSID": "", "__Secure-1PSIDTS": ""}

    with COOKIE_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_cookie_file(psid: str, psidts: str):
    with COOKIE_FILE.open("w", encoding="utf-8") as f:
        json.dump({"__Secure-1PSID": psid, "__Secure-1PSIDTS": psidts}, f, indent=2)


def save_image_locally(image_obj) -> dict:
    """Save a Gemini Image object locally and return metadata with local path.
    
    Args:
        image_obj: A Gemini Image object (either WebImage or GeneratedImage)
    
    Returns:
        dict with url (local path), title, and alt
    """
    try:
        # Generate unique filename
        import uuid
        filename = f"{uuid.uuid4()}.png"
        filepath = IMAGE_STORAGE_DIR / filename
        
        # Synchronously save image
        # Note: Image.save() should be called without await in synchronous context
        if hasattr(image_obj, 'save'):
            try:
                # Try async version first (newer gemini-webapi)
                import inspect
                if inspect.iscoroutinefunction(image_obj.save):
                    # This is async, but we're in sync context - return URL with marker
                    return {
                        "url": "",
                        "title": "Image save pending",
                        "alt": filename
                    }
                else:
                    # Sync version
                    image_obj.save(path=str(IMAGE_STORAGE_DIR), filename=filename)
            except Exception as e:
                print(f"Error saving image: {e}")
                return {
                    "url": "",
                    "title": "Failed to save image",
                    "alt": ""
                }
        
        return {
            "url": f"/serve-image/{filename}",
            "title": getattr(image_obj, 'title', 'Generated Image'),
            "alt": filename
        }
    except Exception as e:
        print(f"Error in save_image_locally: {e}")
        return {
            "url": "",
            "title": "Failed to process image",
            "alt": ""
        }


async def save_image_locally_async(image_obj) -> dict:
    """Async version: Save a Gemini Image object locally and return metadata with local path."""
    try:
        import uuid
        filename = f"{uuid.uuid4()}.png"
        
        # Call the async save method if available
        if hasattr(image_obj, 'save'):
            import inspect
            if inspect.iscoroutinefunction(image_obj.save):
                await image_obj.save(path=str(IMAGE_STORAGE_DIR), filename=filename)
            else:
                image_obj.save(path=str(IMAGE_STORAGE_DIR), filename=filename)
        
        return {
            "url": f"/serve-image/{filename}",
            "title": getattr(image_obj, 'title', 'Generated Image'),
            "alt": filename
        }
    except Exception as e:
        print(f"Error saving image: {e}")
        return {
            "url": "",
            "title": "Failed to save image",
            "alt": ""
        }


async def process_images_for_response_async(response_obj) -> list:
    """Async version: Extract and save images from a Gemini response object."""
    images = []
    if hasattr(response_obj, 'images') and response_obj.images:
        for img in response_obj.images:
            images.append(await save_image_locally_async(img))
    return images


@app.middleware("http")
async def enforce_api_key(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)

    # Skip API key check for health, serve-image, and admin endpoints
    exempt_paths = {"/health", "/serve-image", "/admin.html"}
    if any(request.url.path.startswith(path) for path in exempt_paths):
        return await call_next(request)

    try:
        verify_api_key(request)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return await call_next(request)


# === Endpoints ===

@app.on_event("startup")
async def startup_event():
    """Initialize on startup"""
    print("Gemini API Server starting...")
    if not load_api_key():
        raise RuntimeError("API_KEY is required in environment before startup")
    try:
        await ensure_client()
        print("✓ Gemini client initialized")
    except HTTPException as e:
        print(f"⚠ {e.detail}")
    
    # Start background cleanup task for expired images
    asyncio.create_task(cleanup_expired_images())


async def cleanup_expired_images():
    """Background task to periodically clean up expired temporary images."""
    import time
    while True:
        try:
            await asyncio.sleep(60)  # Check every minute
            current_time = time.time()
            for image_file in IMAGE_STORAGE_DIR.glob("*.png"):
                file_age = current_time - image_file.stat().st_mtime
                if file_age > IMAGE_TTL_SECONDS:
                    image_file.unlink()
                    print(f"Cleaned up expired image: {image_file.name}")
        except Exception as e:
            print(f"Error in image cleanup: {e}")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    global gemini_client
    if gemini_client:
        try:
            await gemini_client.close()
            print("✓ Gemini client closed")
        except Exception as e:
            print(f"Error closing client: {e}")


@app.get("/health", tags=["System"])
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy" if gemini_client else "initializing",
        "client_initialized": gemini_client is not None
    }


@app.get("/serve-image/{filename}", tags=["System"])
async def serve_image(filename: str):
    """Serve a locally-saved temporary image.
    
    Images are auto-deleted after 5 minutes.
    """
    filepath = IMAGE_STORAGE_DIR / filename
    
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Image not found or expired")
    
    # Check if file is too old and delete if expired
    import time
    file_age = time.time() - filepath.stat().st_mtime
    if file_age > IMAGE_TTL_SECONDS:
        filepath.unlink()
        raise HTTPException(status_code=404, detail="Image expired")
    
    return StreamingResponse(
        open(filepath, "rb"),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=300"}
    )


@app.get("/", tags=["System"])
async def get_index():
    """API-only root endpoint"""
    return {"message": "Gemini API Server", "admin": "/admin.html"}


@app.get("/admin.html", response_class=HTMLResponse, tags=["Admin"])
async def admin_page():
    """Password-protected page for viewing and updating cookies.json"""
    return """
        <!doctype html>
        <html>
        <head>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>Gemini Cookie Admin</title>
            <style>
                body { font-family: Arial, sans-serif; margin: 0; background: #0f172a; color: #e2e8f0; }
                .wrap { max-width: 780px; margin: 0 auto; padding: 32px; }
                .card { background: #111827; border: 1px solid #334155; border-radius: 14px; padding: 20px; margin-bottom: 16px; }
                input, textarea, button { width: 100%; box-sizing: border-box; margin-top: 8px; padding: 12px; border-radius: 10px; border: 1px solid #475569; background: #0b1220; color: #e2e8f0; }
                button { background: #22c55e; color: #051b0e; font-weight: 700; cursor: pointer; border: none; }
                button.secondary { background: #38bdf8; color: #06202c; }
                pre { white-space: pre-wrap; word-break: break-word; background: #020617; padding: 12px; border-radius: 10px; }
                .muted { color: #94a3b8; }
                @media (max-width: 720px) { .row { grid-template-columns: 1fr; } }
            </style>
        </head>
        <body>
            <div class="wrap">
                <h1>Gemini Cookie Admin</h1>
                <p class="muted">Update <code>cookies.json</code> on the hosted server. Protect this page with <code>ADMIN_PASSWORD</code>.</p>

                <div class="card">
                    <label>Admin Password</label>
                    <input id="password" type="password" placeholder="Enter admin password" />
                    <button class="secondary" onclick="loadCookies()">Load cookies</button>
                </div>

                <div class="card">
                    <label>__Secure-1PSID</label>
                    <textarea id="psid" rows="4" placeholder="Paste PSID here"></textarea>
                    <label>__Secure-1PSIDTS</label>
                    <textarea id="psidts" rows="4" placeholder="Paste PSIDTS here"></textarea>
                    <label>Raw cookies JSON (optional)</label>
                    <textarea id="rawjson" rows="7" placeholder='{"__Secure-1PSID":"...","__Secure-1PSIDTS":"..."}'></textarea>
                    <button class="secondary" onclick="applyRawJson()">Parse JSON into fields</button>
                    <button onclick="saveCookies()">Save cookies.json</button>
                </div>

                <div class="card">
                    <div id="status" class="muted">Ready.</div>
                    <pre id="output"></pre>
                </div>
            </div>

            <script>
                const apiKeyFromQuery = new URLSearchParams(window.location.search).get('api_key') || '';

                function headers() {
                    return {
                        'Content-Type': 'application/json',
                        'X-API-Key': apiKeyFromQuery,
                        'X-Admin-Password': document.getElementById('password').value
                    };
                }

                function parseCookieJson(text) {
                    const parsed = JSON.parse(text);
                    const psid = parsed['__Secure-1PSID'] || parsed.psid || '';
                    const psidts = parsed['__Secure-1PSIDTS'] || parsed.psidts || '';
                    if (!psid) {
                        throw new Error('JSON must include __Secure-1PSID or psid');
                    }
                    return { psid, psidts };
                }

                function applyRawJson() {
                    try {
                        const raw = document.getElementById('rawjson').value.trim();
                        if (!raw) {
                            throw new Error('Raw JSON is empty');
                        }
                        const values = parseCookieJson(raw);
                        document.getElementById('psid').value = values.psid;
                        document.getElementById('psidts').value = values.psidts;
                        document.getElementById('status').textContent = 'JSON parsed into fields.';
                        document.getElementById('output').textContent = JSON.stringify(values, null, 2);
                    } catch (err) {
                        document.getElementById('status').textContent = err.message || 'Invalid JSON';
                    }
                }

                async function loadCookies() {
                    const response = await fetch('/admin/cookies', { headers: headers() });
                    const data = await response.json();
                    if (!response.ok) {
                        document.getElementById('status').textContent = data.detail || 'Failed to load';
                        return;
                    }
                    document.getElementById('psid').value = data.psid || '';
                    document.getElementById('psidts').value = data.psidts || '';
                    document.getElementById('rawjson').value = JSON.stringify({
                        '__Secure-1PSID': data.psid || '',
                        '__Secure-1PSIDTS': data.psidts || ''
                    }, null, 2);
                    document.getElementById('status').textContent = 'Cookies loaded.';
                    document.getElementById('output').textContent = JSON.stringify(data, null, 2);
                }

                async function saveCookies() {
                    let payload = {
                        psid: document.getElementById('psid').value,
                        psidts: document.getElementById('psidts').value
                    };

                    const raw = document.getElementById('rawjson').value.trim();
                    if (raw) {
                        try {
                            payload = parseCookieJson(raw);
                        } catch (err) {
                            document.getElementById('status').textContent = err.message || 'Invalid JSON';
                            return;
                        }
                    }

                    const response = await fetch('/admin/cookies', {
                        method: 'PUT',
                        headers: headers(),
                        body: JSON.stringify(payload)
                    });
                    const data = await response.json();
                    document.getElementById('status').textContent = response.ok ? 'Cookies saved.' : (data.detail || 'Failed to save');
                    document.getElementById('output').textContent = JSON.stringify(data, null, 2);
                }
            </script>
        </body>
        </html>
        """


@app.get("/admin/cookies", tags=["Admin"])
async def admin_get_cookies(x_admin_password: Optional[str] = Header(default=None)):
        verify_admin_password(x_admin_password or "")
        cookies = read_cookie_file()
        return {
                "psid": cookies.get("__Secure-1PSID", ""),
                "psidts": cookies.get("__Secure-1PSIDTS", "")
        }


@app.put("/admin/cookies", tags=["Admin"])
async def admin_update_cookies(request: CookieUpdateRequest, x_admin_password: Optional[str] = Header(default=None)):
    verify_admin_password(x_admin_password or "")
    if not request.psid.strip():
        raise HTTPException(status_code=400, detail="psid is required")

    psid = request.psid.strip()
    psidts = request.psidts.strip()
    write_cookie_file(psid, psidts)

    global gemini_client
    if gemini_client:
        try:
            await gemini_client.close()
        except Exception:
            pass
        gemini_client = None

    # Reinitialize immediately so the API is ready right after cookie save.
    if not await init_client(psid, psidts):
        raise HTTPException(status_code=500, detail="cookies.json saved, but Gemini client reinitialization failed")

    return {"status": "saved", "message": "cookies.json updated and Gemini client reloaded."}


@app.post("/init", tags=["System"])
async def initialize():
    """Manual init is disabled. Use cookies.json or /admin.html instead."""
    raise HTTPException(status_code=400, detail="Manual init disabled. Update cookies.json instead.")


@app.post("/generate", tags=["Content"])
async def generate_content(request: ContentRequest):
    """
    Generate content from a prompt - OPTIMIZED FOR SPEED
    
    - **prompt**: The prompt to send
    - **model**: Model name (optional)
    - **temporary**: Whether to save in history
    - **stream**: Whether to stream response
    """
    await ensure_client()
    
    try:
        if request.stream:
            # Prevent concurrent stream/generate calls from corrupting the shared client socket state.
            if generation_lock.locked():
                raise HTTPException(
                    status_code=429,
                    detail="Gemini is currently generating another response. Please wait a moment and retry."
                )

            async def generate():
                """Optimized streaming generator with metadata support"""
                kwargs = generation_kwargs(request.model, request.temporary)
                async with generation_lock:
                    full_response = None
                    # Stream chunks as they arrive
                    async for chunk in gemini_client.generate_content_stream(
                        request.prompt,
                        **kwargs
                    ):
                        delta_text = chunk.text_delta
                        # In gemini-webapi, chunk IS a ModelOutput object and 
                        # it accumulates properties like .images as it goes.
                        full_response = chunk
                        yield f"data: {json.dumps({'delta': delta_text})}\n\n".encode()
                    
                    # Check for images or other media in the final response
                    if full_response:
                        images = await process_images_for_response_async(full_response)
                        if images:
                            yield f"data: {json.dumps({'images': images})}\n\n".encode()
                    
                    # Signal completion
                    yield b"data: [DONE]\n\n"
            
            return StreamingResponse(generate(), media_type="text/event-stream")
        else:
            kwargs = generation_kwargs(request.model, request.temporary)
            async with generation_lock:
                response = await gemini_client.generate_content(
                    request.prompt,
                    **kwargs
                )
            
            # Extract images using local storage (async)
            images = await process_images_for_response_async(response)
            
            videos = []
            for vid in response.videos:
                videos.append({
                    "url": getattr(vid, 'url', ''),
                    "title": getattr(vid, 'title', 'Video')
                })
            
            media = []
            for m in response.media:
                media.append({
                    "url": getattr(m, 'url', ''),
                    "title": getattr(m, 'title', 'Media')
                })
            
            return {
                "text": response.text,
                "images": images,
                "videos": videos,
                "media": media,
                "thoughts": getattr(response, 'thoughts', ''),
                "model": getattr(response, 'model', '')
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/chat/start", tags=["Chat"])
async def start_chat(request: ChatSessionRequest):
    """
    Start a new chat session
    
    - **prompt**: Initial message
    - **model**: Model name (optional)
    - **gem**: Gem ID to use (optional)
    """
    await ensure_client()
    
    try:
        # Build kwargs only with non-None values
        kwargs = {}
        if request.model:
            kwargs['model'] = request.model
        if request.gem:
            kwargs['gem'] = request.gem
            
        chat = gemini_client.start_chat(**kwargs)
        async with generation_lock:
            response = await chat.send_message(request.prompt, temporary=request.temporary)
        
        # Store session
        session_id = chat.cid
        sessions[session_id] = chat
        
        # Extract images using local storage (async)
        images = await process_images_for_response_async(response)
        
        # PROMPT OPTIMIZATION: If user is asking for images, help Gemini realize it.
        # This is a hidden system hint applied if no images are found but prompt looks visual.
        visual_keywords = ['generate', 'show', 'draw', 'picture', 'image', 'photo', 'create']
        if not images and any(kw in request.prompt.lower() for kw in visual_keywords):
             print("💡 Visual prompt detected but no images returned - hint: Try asking 'Generate an image of...'")

        videos = []
        for vid in response.videos:
            videos.append({
                "url": getattr(vid, 'url', ''),
                "title": getattr(vid, 'title', 'Video')
            })
        
        media = []
        for m in response.media:
            media.append({
                "url": getattr(m, 'url', ''),
                "title": getattr(m, 'title', 'Media')
            })
        
        return {
            "session_id": session_id,
            "text": response.text,
            "images": images,
            "videos": videos,
            "media": media,
            "model": getattr(response, 'model', '')
        }
    except Exception as e:
        import traceback
        print(f"Error in /chat/start: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Chat error: {str(e)}")


# Store active chat sessions
sessions = {}


@app.post("/chat/reply", tags=["Chat"])
async def chat_reply(request: ChatReplyRequest):
    """
    Continue an existing chat session
    
    - **session_id**: Chat session ID
    - **prompt**: Message to send
    """
    await ensure_client()
    
    if request.session_id not in sessions:
        raise HTTPException(status_code=404, detail=f"Session {request.session_id} not found")
    
    try:
        chat = sessions[request.session_id]
        async with generation_lock:
            response = await chat.send_message(request.prompt, temporary=request.temporary)
        
        # Extract images using local storage (async)
        images = await process_images_for_response_async(response)
        
        videos = []
        for vid in response.videos:
            videos.append({
                "url": getattr(vid, 'url', ''),
                "title": getattr(vid, 'title', 'Video')
            })
        
        media = []
        for m in response.media:
            media.append({
                "url": getattr(m, 'url', ''),
                "title": getattr(m, 'title', 'Media')
            })
        
        return {
            "session_id": request.session_id,
            "text": response.text,
            "images": images,
            "videos": videos,
            "media": media,
            "model": getattr(response, 'model', '')
        }
    except Exception as e:
        import traceback
        print(f"Error in /chat/reply: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Chat error: {str(e)}")


@app.get("/chat/history/{session_id}", tags=["Chat"])
async def get_chat_history(session_id: str):
    """Get conversation history for a chat session"""
    await ensure_client()
    
    try:
        history = await gemini_client.read_chat(session_id)
        if not history:
            return {"turns": []}
        
        turns = []
        for turn in history.turns:
            turns.append({
                "role": turn.role,
                "text": turn.text
            })
        return {"turns": turns}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/chat/list", tags=["Chat"])
async def list_chats():
    """List all recent chats"""
    await ensure_client()
    
    try:
        chats = gemini_client.list_chats()
        if not chats:
            return {"chats": []}
        
        return {
            "chats": [
                {"cid": chat.cid, "title": chat.title}
                for chat in chats
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/chat/{session_id}", tags=["Chat"])
async def delete_chat(session_id: str):
    """Delete a chat from history"""
    await ensure_client()
    
    try:
        await gemini_client.delete_chat(session_id)
        if session_id in sessions:
            del sessions[session_id]
        return {"status": "deleted", "session_id": session_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/models", tags=["Models"])
async def list_models():
    """List available models"""
    await ensure_client()
    
    try:
        print("🔍 Listing available models...")
        # list_models() is NOT async - it's a regular function call
        models = gemini_client.list_models()
        
        print(f"   Found {len(models) if models else 0} model(s)")
        if models:
            for model in models:
                print(f"   - {model.display_name} ({model.model_name})")
        else:
            print("   ⚠️ No models returned - client might not be fully initialized")
            
        return {
            "models": [
                {
                    "model_name": model.model_name,
                    "display_name": model.display_name
                }
                for model in (models or [])
            ]
        }
    except Exception as e:
        import traceback
        print(f"❌ Error listing models: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Models error: {str(e)}")


@app.post("/generate-image", tags=["Images"])
async def generate_image(request: GenerateImageRequest):
    """Generate images with Gemini"""
    await ensure_client()
    
    try:
        kwargs = {}
        if request.model:
            kwargs['model'] = request.model
            
        response = await gemini_client.generate_content(
            request.prompt,
            **kwargs
        )
        
        images = await process_images_for_response_async(response)
        
        return {"images": images}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/upload-files", tags=["Files"])
async def upload_files(
    prompt: str = Query(..., description="Prompt to send with files"),
    files: List[UploadFile] = File(...)
):
    """Generate content with uploaded files"""
    await ensure_client()
    
    try:
        # Save uploaded files temporarily
        temp_files = []
        for file in files:
            with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename).suffix) as tmp:
                content = await file.read()
                tmp.write(content)
                temp_files.append(tmp.name)
        
        # Generate content with files
        response = await gemini_client.generate_content(
            prompt,
            files=temp_files
        )
        
        # Cleanup temp files
        for tmp_file in temp_files:
            os.unlink(tmp_file)
        
        return {
            "text": response.text,
            "images": await process_images_for_response_async(response)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/deep-research", tags=["Research"])
async def deep_research(request: DeepResearchRequest):
    """
    Perform deep research on a query
    
    This is a long-running operation that may take several minutes
    """
    await ensure_client()
    
    try:
        result = await gemini_client.deep_research(
            request.query,
            poll_interval=request.poll_interval,
            timeout=request.timeout
        )
        
        return {
            "done": result.done,
            "text": result.text,
            "title": getattr(result, 'title', None)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/gems/create", tags=["Gems"])
async def create_gem(request: GemCreateRequest):
    """Create a custom gem"""
    await ensure_client()
    
    try:
        gem = await gemini_client.create_gem(
            name=request.name,
            prompt=request.prompt,
            description=request.description
        )
        
        return {
            "id": gem.id,
            "name": gem.name,
            "prompt": gem.prompt,
            "description": gem.description
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/gems", tags=["Gems"])
async def list_gems(include_hidden: bool = False):
    """List all gems"""
    await ensure_client()
    
    try:
        await gemini_client.fetch_gems(include_hidden=include_hidden)
        gems = gemini_client.gems
        
        return {
            "gems": [
                {
                    "id": gem.id,
                    "name": gem.name,
                    "description": gem.description,
                    "predefined": gem.predefined
                }
                for gem in gems
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/gems/{gem_id}", tags=["Gems"])
async def delete_gem(gem_id: str):
    """Delete a custom gem"""
    await ensure_client()
    
    try:
        await gemini_client.delete_gem(gem_id)
        return {"status": "deleted", "gem_id": gem_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/chat/candidates/{session_id}", tags=["Chat"])
async def get_candidates(session_id: str):
    """Get reply candidates for current message in a chat session"""
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    
    try:
        chat = sessions[session_id]
        candidates = chat.response.candidates if hasattr(chat, 'response') and chat.response else []
        
        return {
            "session_id": session_id,
            "candidates_count": len(candidates),
            "candidates": [
                {
                    "index": i,
                    "text": getattr(candidate, 'text', str(candidate))[:200]
                }
                for i, candidate in enumerate(candidates)
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/chat/choose-candidate/{session_id}", tags=["Chat"])
async def choose_candidate(session_id: str, index: int = 0):
    """Choose a specific reply candidate in a chat session"""
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    
    try:
        chat = sessions[session_id]
        chat.choose_candidate(index=index)
        return {
            "session_id": session_id,
            "chosen_candidate": index,
            "message": "Candidate selected. Next message will be based on this candidate."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/logging/set-level", tags=["System"])
async def set_log_level(level: str = "INFO"):
    """
    Set logging level
    
    - **level**: DEBUG, INFO, WARNING, ERROR, CRITICAL
    """
    try:
        from gemini_webapi import set_log_level
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        
        if level.upper() not in valid_levels:
            raise ValueError(f"Invalid level. Must be one of: {', '.join(valid_levels)}")
        
        set_log_level(level.upper())
        print(f"📝 Log level set to {level.upper()}")
        
        return {
            "status": "success",
            "log_level": level.upper()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)