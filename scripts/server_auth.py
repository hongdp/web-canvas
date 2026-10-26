"""Authentication layer: sessions, CSRF, and the /api/auth/* endpoints.

Exposes:
- `csrf_middleware` — registered on the app by api_server via
  `app.middleware("http")(...)`.
- `get_authenticated_username` — session-cookie validation used by every
  authenticated endpoint.
- `router` — an APIRouter with the register/login/logout/session endpoints,
  included into the app by api_server.

Tests that need a fake sessions file should patch
`server_auth.SESSIONS_FILE` (this module reads its own global).
"""

import re
import secrets
import hashlib
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import JSONResponse

from server_config import USERS_FILE, SESSIONS_FILE, load_json_file, save_json_file
from server_db import lookup_last_active_book_id

router = APIRouter()

# ── CSRF Middleware ────────────────────────────────────────────────────────────
async def csrf_middleware(request: Request, call_next):
    # Validate state-changing requests (POST, PUT, DELETE)
    if request.method in ("POST", "PUT", "DELETE"):
        csrf_header = request.headers.get("x-csrf-token")
        csrf_cookie = request.cookies.get("csrf_token")
        if not csrf_cookie or csrf_header != csrf_cookie:
            return JSONResponse(
                status_code=403,
                content={"error": "CSRF token mismatch or missing."}
            )

    return await call_next(request)

# ── Authentication ─────────────────────────────────────────────────────────────
def get_authenticated_username(request: Request) -> str:
    session_id = request.cookies.get("web_canvas_session")
    if not session_id:
        raise HTTPException(status_code=401, detail="Authentication required.")

    sessions = load_json_file(SESSIONS_FILE)
    session = sessions.get(session_id)
    if not session or "username" not in session or "expiresAt" not in session:
        raise HTTPException(status_code=401, detail="Authentication required.")

    try:
        expires_at = datetime.fromisoformat(session["expiresAt"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expires_at:
            raise HTTPException(status_code=401, detail="Session expired.")
    except Exception:
        raise HTTPException(status_code=401, detail="Authentication required.")

    return session["username"]

# 1. Session GET endpoint
@router.get("/api/auth/session")
async def get_session(request: Request, response: Response):
    csrf_token = request.cookies.get("csrf_token")
    if not csrf_token:
        csrf_token = secrets.token_hex(16)
        response.set_cookie(
            key="csrf_token",
            value=csrf_token,
            path="/",
            samesite="lax",
            max_age=365 * 24 * 60 * 60,
            httponly=False,
            secure=True
        )

    session_id = request.cookies.get("web_canvas_session")
    sessions = load_json_file(SESSIONS_FILE)
    session = sessions.get(session_id) if session_id else None

    if session and "username" in session and "expiresAt" in session:
        try:
            expires_at = datetime.fromisoformat(session["expiresAt"].replace("Z", "+00:00"))
            if datetime.now(timezone.utc) <= expires_at:
                return {
                    "loggedIn": True,
                    "username": session["username"],
                    "csrfToken": csrf_token,
                    # The book to open on a device with no local state
                    # (see server_db.record_last_active_book). None until the
                    # account has a book.
                    "lastActiveBookId": lookup_last_active_book_id(session["username"]),
                }
        except Exception:
            pass

    return {"loggedIn": False, "csrfToken": csrf_token}

# 2. Register endpoint
@router.post("/api/auth/register")
async def register_user(request: Request):
    try:
        body = await request.json()
        username = body.get("username")
        password = body.get("password")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    if not username or not re.match(r"^[a-zA-Z0-9_-]{3,30}$", username):
        raise HTTPException(status_code=400, detail="Username must be alphanumeric, between 3 and 30 characters.")

    if not password or len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters long.")

    users = load_json_file(USERS_FILE)
    if username in users:
        raise HTTPException(status_code=400, detail="Username is already taken.")

    salt_bytes = secrets.token_bytes(16)
    salt_hex = salt_bytes.hex()

    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt_bytes,
        n=16384,
        r=8,
        p=1,
        dklen=64
    )
    hash_hex = dk.hex()

    users[username] = {
        "username": username,
        "salt": salt_hex,
        "hash": hash_hex
    }
    save_json_file(USERS_FILE, users)

    return {"success": True}

# 3. Login endpoint
@router.post("/api/auth/login")
async def login_user(request: Request, response: Response):
    try:
        body = await request.json()
        username = body.get("username")
        password = body.get("password")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request.")

    users = load_json_file(USERS_FILE)
    user = users.get(username)
    if not user or "salt" not in user or "hash" not in user:
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    salt_bytes = bytes.fromhex(user["salt"])
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt_bytes,
        n=16384,
        r=8,
        p=1,
        dklen=64
    )
    check_hash_hex = dk.hex()

    if check_hash_hex != user["hash"]:
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    session_id = secrets.token_hex(32)
    sessions = load_json_file(SESSIONS_FILE)
    sessions = {k: v for k, v in sessions.items() if v.get("username") != username}

    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    sessions[session_id] = {
        "username": username,
        "expiresAt": expires_at.isoformat().replace("+00:00", "Z")
    }
    save_json_file(SESSIONS_FILE, sessions)

    response.set_cookie(
        key="web_canvas_session",
        value=session_id,
        httponly=True,
        path="/",
        samesite="lax",
        max_age=7 * 24 * 60 * 60,
        secure=True
    )

    csrf_token = request.cookies.get("csrf_token") or secrets.token_hex(16)
    response.set_cookie(
        key="csrf_token",
        value=csrf_token,
        path="/",
        samesite="lax",
        max_age=365 * 24 * 60 * 60,
        httponly=False,
        secure=True
    )

    return {"success": True, "username": username, "csrfToken": csrf_token}

# 4. Logout endpoint
@router.post("/api/auth/logout")
async def logout_user(request: Request, response: Response):
    session_id = request.cookies.get("web_canvas_session")
    if session_id:
        sessions = load_json_file(SESSIONS_FILE)
        if session_id in sessions:
            del sessions[session_id]
            save_json_file(SESSIONS_FILE, sessions)

    response.delete_cookie(key="web_canvas_session", path="/")
    return {"success": True}
