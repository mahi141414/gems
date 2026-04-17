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
_convex_client: Optional[ConvexClient] = None

client: Optional[GeminiClient] = None
sessions: dict[str, dict] = {}
_session_locks: dict[str, asyncio.Lock] = {}
_client_lock = asyncio.Lock()

_last_psidts: Optional[str] = None
_psidts_stale_count: int = 0
PSIDTS_STALE_THRESHOLD = 3

STALE_SESSION_PATTERNS = ["silently aborted"]


def _is_stale_session_error(exc: Exception) -> bool:
    if not isinstance(exc, APIError):
        return False
    msg = str(exc).lower()
    return any(p.lower() in msg for p in STALE_SESSION_PATTERNS)


async def _force_reinit() -> bool:
    global _last_psidts, _psidts_stale_count
    sessions.clear()
    _session_locks.clear()
    _psidts_stale_count = 0
    _last_psidts = None
    ok = await _try_auto_reinit()
    if ok:
        print("[force_reinit] Client re-initialized successfully")
    else:
        print("[force_reinit] Re-init failed")
    return ok


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
            if current_psidts and current_psidts == _last_psidts:
                _psidts_stale_count += 1
                if _psidts_stale_count >= PSIDTS_STALE_THRESHOLD:
                    print(
                        f"[persist] __Secure-1PSIDTS hasn't rotated in "
                        f"{PSIDTS_STALE_THRESHOLD * PERSIST_INTERVAL}s. "
                        f"Auto-refresh may be failing. Re-initializing..."
                    )
                    if await _force_reinit():
                        _psidts_stale_count = 0
                        _last_psidts = dict(client.cookies).get("__Secure-1PSIDTS", "")
                    else:
                        print("[persist] Re-init failed. Will retry next cycle.")
            else:
                _psidts_stale_count = 0
                _last_psidts = current_psidts


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
    try:
        raw = await _load_cookies_json()
        if not raw:
            return False
        psid, psidts, all_cookies = get_gemini_cookies(str(COOKIES_PATH))
        if not psid:
            return False
        async with _client_lock:
            if client and client._running:
                await client.close()
            client = None
        clear_cookie_cache()
        new_client = GeminiClient(secure_1psid=psid, secure_1psidts=psidts or "")
        new_client.cookies = all_cookies
        await new_client.init(
            timeout=60, auto_close=False, auto_refresh=True, verbose=False
        )
        new_client.cookies = all_cookies
        client = new_client
        return client.account_status == AccountStatus.AVAILABLE
    except Exception:
        client = None
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
    title="Suva Gems API — Gem Endpoint",
    version="1.1.0",
    description=f"OpenAI-compatible API backed by Gemini Gem `{GEM_ID}`. Cookies persisted to Convex.",
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
    sessions.clear()
    _session_locks.clear()

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
# Session management
# ---------------------------------------------------------------------------


@app.post("/v1/sessions", dependencies=[Depends(verify_api_key)])
async def create_session():
    c = await ensure_client()
    try:
        chat = c.start_chat(gem=GEM_ID)
        sid = str(uuid.uuid4())
        sessions[sid] = {
            "chat": chat,
            "created_at": int(time.time()),
            "gemini_chat_id": chat.cid,
        }
        _session_locks[sid] = asyncio.Lock()
        return {
            "id": sid,
            "object": "session",
            "created_at": sessions[sid]["created_at"],
            "gemini_chat_id": chat.cid,
            "gem_id": GEM_ID,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create session: {e}")


@app.get("/v1/sessions", dependencies=[Depends(verify_api_key)])
async def list_sessions():
    return {
        "object": "list",
        "data": [
            {
                "id": sid,
                "object": "session",
                "created_at": s["created_at"],
                "gemini_chat_id": s["gemini_chat_id"],
                "gem_id": GEM_ID,
            }
            for sid, s in sessions.items()
        ],
    }


@app.get("/v1/sessions/{session_id}", dependencies=[Depends(verify_api_key)])
async def get_session(session_id: str):
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    c = await ensure_client()
    s = sessions[session_id]
    chat = s["chat"]

    history_data = []
    try:
        if chat.cid:
            history = await c.read_chat(chat.cid)
            if history and history.turns:
                for turn in reversed(history.turns):
                    history_data.append({"role": turn.role, "content": turn.text or ""})
    except Exception:
        pass

    return {
        "id": session_id,
        "object": "session",
        "created_at": s["created_at"],
        "gemini_chat_id": chat.cid,
        "gem_id": GEM_ID,
        "messages": history_data,
    }


@app.delete("/v1/sessions/{session_id}", dependencies=[Depends(verify_api_key)])
async def delete_session(session_id: str):
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    del sessions[session_id]
    _session_locks.pop(session_id, None)
    return {"id": session_id, "object": "session.deleted", "deleted": True}


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
    session_id: Optional[str] = Field(
        default=None,
        description="Persist session across requests. Create via POST /v1/sessions or let auto-create.",
    )


def _completion_id():
    return "chatcmpl-" + uuid.uuid4().hex[:29]


async def _resolve_session(
    req: ChatCompletionRequest,
) -> tuple[str, ChatSession, bool]:
    c = await ensure_client()
    is_new = False

    if req.session_id and req.session_id in sessions:
        sid = req.session_id
        chat = sessions[sid]["chat"]
    else:
        chat = c.start_chat(gem=GEM_ID)
        sid = str(uuid.uuid4())
        sessions[sid] = {
            "chat": chat,
            "created_at": int(time.time()),
            "gemini_chat_id": chat.cid,
        }
        _session_locks[sid] = asyncio.Lock()
        is_new = True

    return sid, chat, is_new


async def _send_and_format(
    sid: str, chat: ChatSession, prompt: str, req: ChatCompletionRequest
):
    lock = _session_locks.get(sid)
    if lock:
        await lock.acquire()

    try:
        response = await chat.send_message(prompt)
        await persist_cookies()

        completion_id = _completion_id()
        created = int(time.time())
        text = response.text or ""
        candidates = [c.text for c in (response.candidates or []) if c.text]

        result = {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": GEM_ID,
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
            "session_id": sid,
            "gemini_chat_id": chat.cid,
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
    except (APIError, GeminiError) as e:
        if _is_stale_session_error(e):
            print(
                f"[send] Stale session detected: {e}. Re-initializing and retrying..."
            )
            if lock and lock.locked():
                lock.release()
            lock = None
            if await _force_reinit() and client:
                new_chat = client.start_chat(gem=GEM_ID)
                new_sid = str(uuid.uuid4())
                sessions[new_sid] = {
                    "chat": new_chat,
                    "created_at": int(time.time()),
                    "gemini_chat_id": new_chat.cid,
                }
                new_lock = asyncio.Lock()
                _session_locks[new_sid] = new_lock
                await new_lock.acquire()
                try:
                    response = await new_chat.send_message(prompt)
                    await persist_cookies()
                    completion_id = _completion_id()
                    created = int(time.time())
                    text = response.text or ""
                    candidates = [c.text for c in (response.candidates or []) if c.text]
                    result = {
                        "id": completion_id,
                        "object": "chat.completion",
                        "created": created,
                        "model": GEM_ID,
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": text,
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0,
                        },
                        "session_id": new_sid,
                        "gemini_chat_id": new_chat.cid,
                    }
                    if len(candidates) > 1:
                        for i, cand in enumerate(candidates[1:], 1):
                            result["choices"].append(
                                {
                                    "index": i,
                                    "message": {
                                        "role": "assistant",
                                        "content": cand,
                                    },
                                    "finish_reason": "stop",
                                }
                            )
                    return result
                except Exception as e2:
                    _raise_for_gemini_error(e2)
                finally:
                    if new_lock and new_lock.locked():
                        new_lock.release()
        _raise_for_gemini_error(e)
    except Exception as e:
        _raise_for_gemini_error(e)
    finally:
        if lock and lock.locked():
            lock.release()


async def _stream_and_format(
    sid: str, chat: ChatSession, prompt: str, req: ChatCompletionRequest
):
    lock = _session_locks.get(sid)
    if lock:
        await lock.acquire()

    completion_id = _completion_id()
    created = int(time.time())

    async def event_generator():
        nonlocal chat, sid, lock
        retried = False

        try:
            yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': GEM_ID, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"

            async for chunk in chat.send_message_stream(prompt):
                delta = chunk.text_delta or ""
                if delta:
                    yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': GEM_ID, 'choices': [{'index': 0, 'delta': {'content': delta}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"

            yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': GEM_ID, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

            await asyncio.sleep(1.5)
            await persist_cookies()
        except (APIError, GeminiError) as e:
            if not retried and _is_stale_session_error(e):
                retried = True
                print(
                    f"[stream] Stale session detected: {e}. Re-initializing and retrying..."
                )
                if lock and lock.locked():
                    lock.release()
                lock = None

                reinit_ok = await _force_reinit()
                if reinit_ok and client:
                    new_chat = client.start_chat(gem=GEM_ID)
                    new_sid = str(uuid.uuid4())
                    sessions[new_sid] = {
                        "chat": new_chat,
                        "created_at": int(time.time()),
                        "gemini_chat_id": new_chat.cid,
                    }
                    new_lock = asyncio.Lock()
                    _session_locks[new_sid] = new_lock
                    await new_lock.acquire()
                    chat = new_chat
                    sid = new_sid
                    lock = new_lock
                    try:
                        async for chunk in chat.send_message_stream(prompt):
                            delta = chunk.text_delta or ""
                            if delta:
                                yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': GEM_ID, 'choices': [{'index': 0, 'delta': {'content': delta}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"

                        yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': GEM_ID, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"

                        await asyncio.sleep(1.5)
                        await persist_cookies()
                    except Exception as e2:
                        yield f"data: {json.dumps({'error': {'message': str(e2), 'type': 'api_error', 'code': '502'}}, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                    finally:
                        if lock and lock.locked():
                            lock.release()
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
        finally:
            if lock and lock.locked():
                lock.release()

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

    sid, chat, _ = await _resolve_session(req)

    if req.stream:
        return await _stream_and_format(sid, chat, last_user_msg, req)
    return await _send_and_format(sid, chat, last_user_msg, req)


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
    sessions.clear()
    _session_locks.clear()


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("GEM_PORT", "8001"))
    uvicorn.run(app, host="0.0.0.0", port=port)
