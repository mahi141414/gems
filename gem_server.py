import asyncio
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from convex import ConvexClient
from dotenv import load_dotenv
from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from gemini_webapi import GeminiClient, ChatSession, set_log_level
from gemini_webapi.constants import AccountStatus, Model
from gemini_webapi.exceptions import (
    APIError,
    AuthError,
    GeminiError,
    ModelInvalid,
    TemporarilyBlocked,
    TimeoutError,
    UsageLimitExceeded,
)

from cookies_loader import get_gemini_cookies

load_dotenv()

API_KEY = os.getenv("GEM_API_KEY", "") or os.getenv("API_KEY", "")
GEM_ID = os.getenv("GEM_ID", "fcf6f88e78ea")
COOKIES_PATH = Path(__file__).parent / "cookies.json"
UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
COOKIE_CACHE_DIR = Path(tempfile.gettempdir()) / "gemini_webapi"

CONVEX_URL = os.getenv("CONVEX_URL", "").rstrip("/")
BROWSER_REFRESH = os.getenv("BROWSER_REFRESH", "true").lower() in ("true", "1", "yes")
_convex_client: Optional[ConvexClient] = None

client: Optional[GeminiClient] = None
_client_lock = asyncio.Lock()

_last_psidts: Optional[str] = None
_psidts_stale_count: int = 0
PSIDTS_STALE_THRESHOLD = 3
PSIDTS_PROACTIVE_REFRESH = 6

STALE_SESSION_PATTERNS = ["silently aborted"]


def _is_stale_session_error(exc: Exception) -> bool:
    if not isinstance(exc, APIError):
        return False
    msg = str(exc).lower()
    return any(p.lower() in msg for p in STALE_SESSION_PATTERNS)


def _client_is_alive(c: Optional[GeminiClient]) -> bool:
    return c is not None and c._running and c.account_status == AccountStatus.AVAILABLE


async def _force_reinit() -> bool:
    global _last_psidts, _psidts_stale_count
    _psidts_stale_count = 0
    _last_psidts = None
    ok = await _try_auto_reinit()
    if ok:
        _last_psidts = dict(client.cookies).get("__Secure-1PSIDTS", "")
        print("[force_reinit] Client re-initialized successfully")
        return True

    if not _client_is_alive(client):
        print("[force_reinit] Old client is also dead. Trying browser refresh...")
        if await _browser_refresh_cookies():
            _last_psidts = dict(client.cookies).get("__Secure-1PSIDTS", "")
            print("[force_reinit] Browser refresh succeeded")
            return True

    print(
        "[force_reinit] All recovery methods failed. Old client preserved if running."
    )
    return False


async def _browser_refresh_cookies() -> bool:
    if not BROWSER_REFRESH:
        print("[browser] Browser refresh disabled via BROWSER_REFRESH env var")
        return False
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print(
            "[browser] Playwright not installed. Run: pip install playwright && playwright install chromium"
        )
        return False

    raw = await _load_cookies_json()
    if not raw:
        print("[browser] No cookies to load into browser")
        return False

    try:
        cookies_data = json.loads(raw)
    except Exception:
        print("[browser] Invalid cookies JSON")
        return False

    pw_cookies = []
    if isinstance(cookies_data, list):
        for c in cookies_data:
            if isinstance(c, dict) and c.get("name"):
                pw_cookies.append(
                    {
                        "name": c["name"],
                        "value": c.get("value", ""),
                        "domain": c.get("domain", ".google.com"),
                        "path": c.get("path", "/"),
                        "secure": c.get("secure", True),
                        "httpOnly": c.get("httpOnly", False),
                    }
                )
    elif isinstance(cookies_data, dict):
        for name, value in cookies_data.items():
            if isinstance(value, str):
                pw_cookies.append(
                    {
                        "name": name,
                        "value": value,
                        "domain": ".google.com",
                        "path": "/",
                        "secure": True,
                    }
                )

    if not pw_cookies:
        print("[browser] No cookies to inject")
        return False

    print(f"[browser] Launching headless Chromium with {len(pw_cookies)} cookies...")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
            await context.add_cookies(pw_cookies)

            page = await context.new_page()
            response = await page.goto(
                "https://gemini.google.com/app",
                wait_until="networkidle",
                timeout=30000,
            )

            if response and response.status == 401:
                print(
                    "[browser] Page returned 401 — cookies expired. Need manual re-upload."
                )
                await browser.close()
                return False

            await page.wait_for_timeout(3000)

            browser_cookies = await context.cookies()
            await browser.close()

        google_cookies = [
            c for c in browser_cookies if "google.com" in c.get("domain", "")
        ]
        if not google_cookies:
            print("[browser] No google.com cookies returned")
            return False

        new_psidts = next(
            (c["value"] for c in google_cookies if c["name"] == "__Secure-1PSIDTS"),
            None,
        )
        print(
            f"[browser] Got {len(google_cookies)} cookies. PSIDTS={'refreshed' if new_psidts else 'missing'}"
        )

        cookie_editor_format = []
        for c in google_cookies:
            cookie_editor_format.append(
                {
                    "domain": c.get("domain", ".google.com"),
                    "expirationDate": c.get("expires", -1)
                    if c.get("expires", -1) > 0
                    else None,
                    "hostOnly": not c.get("domain", "").startswith("."),
                    "httpOnly": c.get("httpOnly", False),
                    "name": c["name"],
                    "path": c.get("path", "/"),
                    "sameSite": c.get("sameSite", "None"),
                    "secure": c.get("secure", True),
                    "session": c.get("expires", -1) == -1,
                    "storeId": None,
                    "value": c["value"],
                }
            )

        COOKIES_PATH.write_text(
            json.dumps(cookie_editor_format, indent=2), encoding="utf-8"
        )
        if CONVEX_URL:
            await convex_set_cookies(COOKIES_PATH.read_text(encoding="utf-8"))

        return await _try_auto_reinit()

    except Exception as e:
        print(f"[browser] Refresh failed: {e}")
        return False


def _raise_for_gemini_error(e: Exception):
    if isinstance(e, UsageLimitExceeded):
        raise HTTPException(status_code=429, detail=str(e))
    if isinstance(e, TimeoutError):
        raise HTTPException(status_code=504, detail=str(e))
    if isinstance(e, (APIError, GeminiError)):
        raise HTTPException(status_code=502, detail=str(e))
    if isinstance(e, ModelInvalid):
        raise HTTPException(status_code=400, detail=str(e))
    if isinstance(e, TemporarilyBlocked):
        raise HTTPException(status_code=403, detail=str(e))
    raise HTTPException(status_code=500, detail=str(e))


def clear_cookie_cache():
    if COOKIE_CACHE_DIR.exists():
        for f in COOKIE_CACHE_DIR.iterdir():
            f.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Convex cookie store
# ---------------------------------------------------------------------------


def _get_convex() -> ConvexClient | None:
    global _convex_client
    if not CONVEX_URL:
        return None
    if _convex_client is None:
        _convex_client = ConvexClient(CONVEX_URL)
    return _convex_client


async def convex_get_cookies() -> str | None:
    cvx = _get_convex()
    if not cvx:
        return None
    try:
        result = cvx.query("cookies:get")
        if result and isinstance(result, dict):
            return result.get("data")
        return None
    except Exception:
        return None


async def convex_set_cookies(raw_json: str) -> bool:
    cvx = _get_convex()
    if not cvx:
        return False
    try:
        cvx.mutation("cookies:set", {"data": raw_json})
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Cookie persistence (local + Convex)
# ---------------------------------------------------------------------------


def _persist_local(current: dict[str, str]):
    try:
        try:
            existing = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
        except Exception:
            existing = []
        if isinstance(existing, list):
            by_name = {}
            for item in existing:
                if isinstance(item, dict) and "name" in item:
                    by_name[item["name"]] = item
            for name, value in current.items():
                if name in by_name:
                    by_name[name]["value"] = value
                else:
                    existing.append(
                        {
                            "domain": ".google.com",
                            "hostOnly": False,
                            "httpOnly": False,
                            "name": name,
                            "path": "/",
                            "sameSite": None,
                            "secure": True,
                            "session": False,
                            "storeId": None,
                            "value": value,
                        }
                    )
            COOKIES_PATH.write_text(json.dumps(existing, indent=4), encoding="utf-8")
        elif isinstance(existing, dict):
            existing.update(current)
            COOKIES_PATH.write_text(json.dumps(existing, indent=4), encoding="utf-8")
    except Exception:
        pass


async def persist_cookies():
    if not client or not client._running:
        return
    try:
        current = dict(client.cookies)
        if not current:
            return
        _persist_local(current)
        if CONVEX_URL:
            raw = COOKIES_PATH.read_text(encoding="utf-8")
            await convex_set_cookies(raw)
    except Exception:
        pass


_persist_task: Optional[asyncio.Task] = None
PERSIST_INTERVAL = 300


async def _persist_cookies_loop():
    global _last_psidts, _psidts_stale_count
    while True:
        await asyncio.sleep(PERSIST_INTERVAL)
        if client and client._running:
            await persist_cookies()
            current_psidts = dict(client.cookies).get("__Secure-1PSIDTS", "")
            psidts_stalled = current_psidts and current_psidts == _last_psidts
            if psidts_stalled:
                _psidts_stale_count += 1
            else:
                _psidts_stale_count = 0
                _last_psidts = current_psidts

            if _psidts_stale_count >= PSIDTS_PROACTIVE_REFRESH:
                print(
                    f"[persist] __Secure-1PSIDTS hasn't rotated in "
                    f"{_psidts_stale_count * PERSIST_INTERVAL}s. "
                    f"Proactively refreshing cookies via browser (session will die soon)..."
                )
                if await _browser_refresh_cookies():
                    _psidts_stale_count = 0
                    print("[persist] Proactive browser refresh succeeded.")
                else:
                    print(
                        "[persist] Proactive browser refresh failed. Will retry next cycle."
                    )

            elif _psidts_stale_count >= PSIDTS_STALE_THRESHOLD:
                print(
                    f"[persist] __Secure-1PSIDTS hasn't rotated in "
                    f"{_psidts_stale_count * PERSIST_INTERVAL}s. "
                    f"Probing session health..."
                )
                try:
                    await client._fetch_user_status()
                    if client.account_status != AccountStatus.AVAILABLE:
                        print(
                            f"[persist] Account status: {client.account_status.name}. Re-initializing..."
                        )
                        if await _force_reinit():
                            _psidts_stale_count = 0
                        else:
                            print("[persist] Re-init failed. Will retry next cycle.")
                    else:
                        stale_mins = _psidts_stale_count * PERSIST_INTERVAL // 60
                        next_proactive = (
                            (PSIDTS_PROACTIVE_REFRESH - _psidts_stale_count)
                            * PERSIST_INTERVAL
                            // 60
                        )
                        print(
                            f"[persist] Status: AVAILABLE but PSIDTS stale for {stale_mins}m. "
                            f"Proactive browser refresh in {next_proactive}m."
                        )
                except Exception as e:
                    print(f"[persist] Status probe failed: {e}. Re-initializing...")
                    if await _force_reinit():
                        _psidts_stale_count = 0
                    else:
                        print("[persist] Re-init failed. Will retry next cycle.")


# ---------------------------------------------------------------------------
# Load cookies from Convex → local fallback
# ---------------------------------------------------------------------------


async def _load_cookies_json() -> str | None:
    if CONVEX_URL:
        convex_data = await convex_get_cookies()
        if convex_data:
            COOKIES_PATH.write_text(convex_data, encoding="utf-8")
            return convex_data
    if COOKIES_PATH.exists():
        return COOKIES_PATH.read_text(encoding="utf-8")
    return None


# ---------------------------------------------------------------------------
# Client init / auto-reinit
# ---------------------------------------------------------------------------


async def _try_auto_reinit():
    global client
    old_client = client
    try:
        raw = await _load_cookies_json()
        if not raw:
            return False
        psid, psidts, all_cookies = get_gemini_cookies(str(COOKIES_PATH))
        if not psid:
            return False
        clear_cookie_cache()
        new_client = GeminiClient(secure_1psid=psid, secure_1psidts=psidts or "")
        new_client.cookies = all_cookies
        await new_client.init(
            timeout=60, auto_close=False, auto_refresh=True, verbose=False
        )
        new_client.cookies = all_cookies
        if new_client.account_status != AccountStatus.AVAILABLE:
            print(
                f"[auto_reinit] New client status={new_client.account_status.name}. Discarding."
            )
            await new_client.close()
            if not _client_is_alive(old_client):
                async with _client_lock:
                    if old_client and old_client._running:
                        await old_client.close()
                    client = None
            return False
        async with _client_lock:
            client = new_client
            if old_client and old_client._running:
                await old_client.close()
        await persist_cookies()
        return True
    except Exception as e:
        print(f"[auto_reinit] Failed: {e}")
        if not _client_is_alive(old_client):
            async with _client_lock:
                if old_client and old_client._running:
                    await old_client.close()
                client = None
        else:
            async with _client_lock:
                if client is None:
                    client = old_client
        return False


async def ensure_client():
    global client
    if client is None or not client._running:
        if await _try_auto_reinit():
            return client
        raise HTTPException(
            status_code=503,
            detail="Client not initialized. Upload cookies via /cookies/update",
        )
    if client.account_status != AccountStatus.AVAILABLE:
        if await _try_auto_reinit():
            return client
        raise HTTPException(
            status_code=401,
            detail=f"Session expired (status: {client.account_status.name}). Upload fresh cookies via /cookies/update",
        )
    if _psidts_stale_count >= PSIDTS_STALE_THRESHOLD:
        print("[ensure_client] PSIDTS stale threshold reached. Proactive re-init...")
        if await _force_reinit():
            return client
        raise HTTPException(
            status_code=503,
            detail="Session stale (auto-refresh failing). Upload fresh cookies via /cookies/update",
        )
    return client


def verify_api_key(request: Request):
    if not API_KEY:
        return
    key = request.headers.get("X-API-Key") or request.query_params.get("api_key") or ""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        key = key or auth_header[7:]
    if key != API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key. Pass via X-API-Key header, Authorization: Bearer <key>, or api_key query param.",
        )


app = FastAPI(
    title="Gems API — Gem Endpoint",
    version="3.0.0",
    description=f"OpenAI-compatible API backed by Gemini Gems. Stateless — chat_metadata is Gemini's own. Cookies persisted to Convex.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health & operational
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    if client is None or not client._running:
        recovered = await _try_auto_reinit()
        if recovered:
            return {
                "status": "healthy",
                "client_initialized": True,
                "account_status": client.account_status.name,
                "auto_recovered": True,
                "cookie_source": "convex" if CONVEX_URL else "local",
            }
        return {
            "status": "down",
            "client_initialized": False,
            "message": "Auto-reinit failed. Upload cookies via /cookies/update",
        }

    if client.account_status != AccountStatus.AVAILABLE:
        prev = client.account_status.name
        recovered = await _try_auto_reinit()
        if recovered:
            return {
                "status": "healthy",
                "client_initialized": True,
                "account_status": client.account_status.name,
                "auto_recovered": True,
                "previous_status": prev,
            }
        return {
            "status": "degraded",
            "client_initialized": True,
            "account_status": client.account_status.name,
            "message": "Session not authenticated. Upload fresh cookies via /cookies/update",
        }

    return {
        "status": "healthy",
        "client_initialized": True,
        "account_status": client.account_status.name,
        "cookie_source": "convex" if CONVEX_URL else "local",
    }


@app.post("/cookies/update", dependencies=[Depends(verify_api_key)])
async def update_cookies(file: UploadFile = File(...)):
    content = await file.read()
    try:
        json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    COOKIES_PATH.write_bytes(content)
    psid, _, _ = get_gemini_cookies(str(COOKIES_PATH))
    if not psid:
        raise HTTPException(
            status_code=400, detail="No __Secure-1PSID found in uploaded cookies"
        )

    if CONVEX_URL:
        await convex_set_cookies(content.decode("utf-8"))

    try:
        if await _try_auto_reinit():
            return {
                "status": "cookies_updated",
                "message": "Cookies applied and client reinitialized",
                "persisted_to": "convex+local" if CONVEX_URL else "local",
            }
    except Exception:
        pass
    return {
        "status": "cookies_updated",
        "message": "Cookies saved. Auto-reinit pending.",
        "persisted_to": "convex+local" if CONVEX_URL else "local",
    }


@app.post("/reinit", dependencies=[Depends(verify_api_key)])
async def reinit_client():
    global client
    async with _client_lock:
        if client and client._running:
            await client.close()
        client = None

    raw = await _load_cookies_json()
    psid, psidts, all_cookies = get_gemini_cookies(str(COOKIES_PATH))
    if not psid:
        raise HTTPException(status_code=400, detail="Missing __Secure-1PSID in cookies")

    clear_cookie_cache()
    client = GeminiClient(secure_1psid=psid, secure_1psidts=psidts or "")
    client.cookies = all_cookies
    set_log_level("WARNING")

    try:
        await client.init(
            timeout=300, auto_close=False, auto_refresh=True, verbose=False
        )
        client.cookies = all_cookies
    except AuthError as e:
        client = None
        raise HTTPException(status_code=401, detail=f"Authentication failed: {e}")
    except Exception as e:
        client = None
        raise HTTPException(status_code=500, detail=f"Reinit failed: {e}")

    if client.account_status != AccountStatus.AVAILABLE:
        return {
            "status": "warning",
            "account_status": client.account_status.name,
            "message": client.account_status.description,
        }
    return {
        "status": "reinitialized",
        "message": "Client re-initialized",
        "cookie_source": "convex" if CONVEX_URL else "local",
    }


@app.get("/v1/models")
async def list_models():
    c = await ensure_client()
    models = c.list_models()
    if not models:
        return {"object": "list", "data": []}
    return {
        "object": "list",
        "data": [
            {
                "id": m.model_id,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "google",
                "permission": [],
                "root": m.model_id,
            }
            for m in models
        ],
    }


# ---------------------------------------------------------------------------
# Chat history (stateless — reads directly from Gemini by chat_id)
# ---------------------------------------------------------------------------


@app.get("/v1/chats", dependencies=[Depends(verify_api_key)])
async def list_chats():
    c = await ensure_client()
    try:
        chats = c.list_chats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list chats: {e}")
    if not chats:
        return {"object": "list", "data": []}
    return {
        "object": "list",
        "data": [
            {
                "id": ch.cid,
                "title": getattr(ch, "title", ""),
            }
            for ch in chats
            if ch.cid
        ],
    }


@app.get("/v1/chats/{chat_id}", dependencies=[Depends(verify_api_key)])
async def get_chat_history(chat_id: str):
    gemini_cid = chat_id
    c = await ensure_client()
    try:
        history = await c.read_chat(gemini_cid)
    except Exception as e:
        raise HTTPException(
            status_code=404, detail=f"Chat not found or unreadable: {e}"
        )

    messages = []
    if history and history.turns:
        for turn in reversed(history.turns):
            messages.append({"role": turn.role, "content": turn.text or ""})

    return {
        "id": gemini_cid,
        "object": "chat.history",
        "messages": messages,
    }


@app.delete("/v1/chats/{chat_id}", dependencies=[Depends(verify_api_key)])
async def delete_chat(chat_id: str):
    c = await ensure_client()
    try:
        await c.delete_chat(chat_id)
    except Exception as e:
        raise HTTPException(
            status_code=404, detail=f"Chat not found or undeletable: {e}"
        )
    return {"id": chat_id, "object": "chat.deleted"}


# ---------------------------------------------------------------------------
# OpenAI-compatible chat completions
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = Field(
        default="gem", description="Ignored; always uses the configured gem"
    )
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: bool = False
    gem_id: Optional[str] = Field(
        default=None,
        description=f"Gem ID to use. Defaults to {GEM_ID}.",
    )
    metadata: Optional[list] = Field(
        default=None,
        description="Chat metadata list from a previous response to continue a conversation. Omit to start a new chat.",
    )


def _completion_id():
    return "chatcmpl-" + uuid.uuid4().hex[:29]


def _extract_metadata(chat: ChatSession | None) -> list:
    if chat is None:
        return []
    try:
        meta = chat.metadata
        if meta is not None:
            return list(meta)
    except Exception:
        pass
    return [chat.cid or "", chat.rid or "", chat.rcid or ""]


def _normalize_metadata(meta: list | dict | None) -> list | None:
    if meta is None:
        return None
    if isinstance(meta, list):
        return meta
    if isinstance(meta, dict):
        result = ["", "", ""]
        if "cid" in meta:
            result[0] = meta["cid"]
        elif 0 in meta:
            result[0] = meta[0]
        if "rid" in meta:
            result[1] = meta["rid"]
        elif 1 in meta:
            result[1] = meta[1]
        if "rcid" in meta:
            result[2] = meta["rcid"]
        elif 2 in meta:
            result[2] = meta[2]
        for i in range(3, 10):
            result.append(meta.get(i))
        return result
    return None


def _build_result(
    completion_id: str,
    created: int,
    gem_id: str,
    chat: ChatSession,
    text: str,
    candidates: list[str],
) -> dict:
    result = {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": gem_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        "chat_metadata": _extract_metadata(chat),
    }
    if len(candidates) > 1:
        for i, cand in enumerate(candidates[1:], 1):
            result["choices"].append(
                {
                    "index": i,
                    "message": {"role": "assistant", "content": cand},
                    "finish_reason": "stop",
                }
            )
    return result


async def _send_and_format(chat: ChatSession, prompt: str, gem_id: str):
    try:
        response = await chat.send_message(prompt)
        await persist_cookies()

        text = response.text or ""
        candidates = [c.text for c in (response.candidates or []) if c.text]

        return _build_result(
            _completion_id(), int(time.time()), gem_id, chat, text, candidates
        )
    except (APIError, GeminiError) as e:
        if _is_stale_session_error(e):
            print(
                f"[send] Stale session detected: {e}. Re-initializing and retrying..."
            )
            if await _force_reinit() and client:
                new_chat = client.start_chat(gem=gem_id)
                try:
                    response = await new_chat.send_message(prompt)
                    await persist_cookies()
                    text = response.text or ""
                    candidates = [c.text for c in (response.candidates or []) if c.text]
                    return _build_result(
                        _completion_id(),
                        int(time.time()),
                        gem_id,
                        new_chat,
                        text,
                        candidates,
                    )
                except Exception as e2:
                    _raise_for_gemini_error(e2)
        _raise_for_gemini_error(e)
    except Exception as e:
        _raise_for_gemini_error(e)


async def _stream_and_format(chat: ChatSession, prompt: str, gem_id: str):
    completion_id = _completion_id()
    created = int(time.time())

    def _chunk(delta: dict, finish_reason=None, chat: ChatSession = None) -> str:
        meta = _extract_metadata(chat)
        return f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': gem_id, 'chat_metadata': meta, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish_reason}]}, ensure_ascii=False)}\n\n"

    async def event_generator():
        retried = False
        current_chat = chat

        try:
            yield _chunk({"role": "assistant", "content": ""}, None, current_chat)

            async for chunk in current_chat.send_message_stream(prompt):
                delta = chunk.text_delta or ""
                if delta:
                    yield _chunk({"content": delta}, None, current_chat)

            yield _chunk({}, "stop", current_chat)
            yield "data: [DONE]\n\n"

            await asyncio.sleep(1.5)
            await persist_cookies()
        except (APIError, GeminiError) as e:
            if not retried and _is_stale_session_error(e):
                retried = True
                print(
                    f"[stream] Stale session detected: {e}. Re-initializing and retrying..."
                )

                reinit_ok = await _force_reinit()
                if reinit_ok and client:
                    current_chat = client.start_chat(gem=gem_id)
                    try:
                        async for chunk in current_chat.send_message_stream(prompt):
                            delta = chunk.text_delta or ""
                            if delta:
                                yield _chunk({"content": delta}, None, current_chat)

                        yield _chunk({}, "stop", current_chat)
                        yield "data: [DONE]\n\n"

                        await asyncio.sleep(1.5)
                        await persist_cookies()
                        return
                    except Exception as e2:
                        yield f"data: {json.dumps({'error': {'message': str(e2), 'type': 'api_error', 'code': '502'}}, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return

            yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'api_error', 'code': '502'}}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except UsageLimitExceeded as e:
            yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'rate_limit', 'code': '429'}}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except TimeoutError as e:
            yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'timeout', 'code': '504'}}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'unknown', 'code': '500'}}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
async def chat_completions(req: ChatCompletionRequest):
    last_user_msg = ""
    for m in reversed(req.messages):
        if m.role == "user":
            last_user_msg = m.content
            break
    if not last_user_msg:
        raise HTTPException(
            status_code=400, detail="No user message found in messages array"
        )

    c = await ensure_client()
    gem_id = req.gem_id or GEM_ID

    if req.metadata:
        normalized = _normalize_metadata(req.metadata)
        chat = c.start_chat(metadata=normalized, gem=gem_id)
    else:
        chat = c.start_chat(gem=gem_id)

    if req.stream:
        return await _stream_and_format(chat, last_user_msg, gem_id)
    return await _send_and_format(chat, last_user_msg, gem_id)


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def startup():
    global client, _persist_task, _last_psidts
    raw = await _load_cookies_json()
    if raw:
        try:
            psid, psidts, all_cookies = get_gemini_cookies(str(COOKIES_PATH))
            if psid:
                clear_cookie_cache()
                client = GeminiClient(secure_1psid=psid, secure_1psidts=psidts or "")
                client.cookies = all_cookies
                await client.init(
                    timeout=60,
                    auto_close=False,
                    auto_refresh=True,
                    verbose=False,
                )
                client.cookies = all_cookies
                src = "Convex" if CONVEX_URL else "local"
                if client.account_status == AccountStatus.AVAILABLE:
                    print(f"[startup] Auto-initialized from {src}")
                    _last_psidts = dict(client.cookies).get("__Secure-1PSIDTS", "")
                    await persist_cookies()
                else:
                    print(
                        f"[startup] Client started but status={client.account_status.name}"
                    )
        except Exception as e:
            client = None
            print(f"[startup] Auto-init failed: {e}")
    else:
        print("[startup] No cookies found. Upload via /cookies/update")
    _persist_task = asyncio.create_task(_persist_cookies_loop())


@app.on_event("shutdown")
async def shutdown():
    global client, _persist_task
    if _persist_task:
        _persist_task.cancel()
        _persist_task = None
    if client:
        await persist_cookies()
        await client.close()
        client = None


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("GEM_PORT", "8001"))
    uvicorn.run(app, host="0.0.0.0", port=port)
