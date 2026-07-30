"""
BSE Announcements & Equity API server with User Authentication.
"""

import json
import logging
import os
import re
import sqlite3
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, time, timezone

import jwt
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException, Query, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr, Field
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_auth_requests

from announcement import update
from get_business_standard_response import build_reports_payload

from dotenv import load_dotenv

# Load .env from this file's directory, not the process's cwd — the server must
# start the same way regardless of where uvicorn is invoked from.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("sodhani.auth")

# ── Security & Auth Config ────────────────────────────────────────────────────────
# No fallback value: an app-wide signing secret must never have a public default.
# Raised as a clear message because uvicorn reports any import-time failure as an
# opaque 'Could not import module "server"'.
JWT_SECRET = os.environ.get("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET is not set. Add it to sodhani-server/.env (local) or the "
        "deployment's environment variables. Generate one with: "
        "python -c \"import secrets; print(secrets.token_hex(32))\""
    )
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24 * 7  # 7 days

MSG91_AUTH_KEY = os.environ.get("MSG91_AUTH_KEY", "")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")

def _google_client_id_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID) and "your_google_client_id_here" not in GOOGLE_CLIENT_ID

security_scheme = HTTPBearer(auto_error=False)

def create_access_token(user_id: str, identifier: str, token_version: int = 1) -> str:
    expires = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION_HOURS)
    payload = {
        "sub": user_id,
        "identifier": identifier,
        "tv": token_version,
        "exp": expires
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

_USER_FIELDS = "id, name, age, email, phone_number, created_at"

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security_scheme)) -> dict:
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id: str = payload.get("sub")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload",
                headers={"WWW-Authenticate": "Bearer"},
            )
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(f"SELECT {_USER_FIELDS}, token_version FROM users WHERE id = ?", (user_id,)).fetchone()
        conn.close()
        if not row:
            raise HTTPException(status_code=401, detail="User not found")
        if payload.get("tv") != row["token_version"]:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has been revoked, please log in again",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user = dict(row)
        del user["token_version"]
        return user
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired or invalid",
            headers={"WWW-Authenticate": "Bearer"},
        )

def get_optional_user(credentials: HTTPAuthorizationCredentials = Depends(security_scheme)) -> dict | None:
    if not credentials or not credentials.credentials:
        return None
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id: str = payload.get("sub")
        if not user_id:
            return None
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(f"SELECT {_USER_FIELDS}, token_version FROM users WHERE id = ?", (user_id,)).fetchone()
        conn.close()
        if not row or payload.get("tv") != row["token_version"]:
            return None
        user = dict(row)
        del user["token_version"]
        return user
    except jwt.PyJWTError:
        return None

# ── Paths ────────────────────────────────────────────────────────────────────────

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", APP_DIR)
DB_PATH = os.path.join(DATA_DIR, "bse_announcements.db")
OUTPUT_BASE_DIR = os.environ.get("OUTPUT_BASE_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or APP_DIR
OUTPUT_DIR = os.path.join(OUTPUT_BASE_DIR, "output")
OUTPUT_CONSOLIDATED_DIR = os.path.join(OUTPUT_BASE_DIR, "output_consolidated")
RESEARCH_REPORT_CACHE_TTL_SECONDS = int(os.environ.get("RESEARCH_REPORT_CACHE_TTL_SECONDS", "21600"))
_research_reports_cache: dict[tuple[int], tuple[datetime, dict]] = {}

# ── DB Init ─────────────────────────────────────────────────────────────────────

def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def init_db() -> None:
    """Ensure the announcements and users tables exist."""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = get_db_connection()

    # Migrate from old schema if needed
    cursor = conn.execute("PRAGMA table_info(announcements)")
    existing = {row[1] for row in cursor.fetchall()}
    if existing and "categoryname" not in existing:
        conn.execute("DROP TABLE announcements")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS announcements (
            newsid            TEXT PRIMARY KEY,
            scrip_cd          TEXT,
            news_dt           TEXT,
            newssub           TEXT,
            headline          TEXT,
            slongname         TEXT,
            announcement_type TEXT,
            attachmentname    TEXT,
            categoryname      TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scrip_cd ON announcements(scrip_cd)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_dt ON announcements(news_dt)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_categoryname ON announcements(categoryname)")

    # Users Table Schema Migration
    cursor_user = conn.execute("PRAGMA table_info(users)")
    user_col_rows = cursor_user.fetchall()  # (cid, name, type, notnull, dflt_value, pk)
    user_cols = {row[1] for row in user_col_rows}
    phone_is_not_null = any(row[1] == "phone_number" and row[3] == 1 for row in user_col_rows)
    needs_token_version = bool(user_cols) and "token_version" not in user_cols

    if user_col_rows and (phone_is_not_null or needs_token_version):
        # SQLite has no ALTER COLUMN — rebuild the table to relax phone_number's
        # NOT NULL (Google-only accounts have no real phone number) and add
        # token_version (bumped on logout to revoke outstanding JWTs), preserving
        # existing rows.
        conn.execute("""
            CREATE TABLE users_new (
                id             TEXT PRIMARY KEY,
                name           TEXT NOT NULL,
                age            INTEGER,
                email          TEXT UNIQUE,
                phone_number   TEXT UNIQUE,
                phone_verified INTEGER DEFAULT 1,
                google_id      TEXT UNIQUE,
                token_version  INTEGER NOT NULL DEFAULT 1,
                created_at     TEXT NOT NULL
            )
        """)
        token_version_select = "token_version" if "token_version" in user_cols else "1"
        conn.execute(f"""
            INSERT INTO users_new (id, name, age, email, phone_number, phone_verified, google_id, token_version, created_at)
            SELECT id, name, age, email, NULLIF(phone_number, ''), phone_verified, google_id, {token_version_select}, created_at
            FROM users
        """)
        # Old rows used a synthetic "google_<uuid>" placeholder since phone_number
        # was NOT NULL; now that it's nullable, drop the placeholder.
        conn.execute("UPDATE users_new SET phone_number = NULL WHERE phone_number LIKE 'google\\_%' ESCAPE '\\'")
        conn.execute("DROP TABLE users")
        conn.execute("ALTER TABLE users_new RENAME TO users")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id             TEXT PRIMARY KEY,
            name           TEXT NOT NULL,
            age            INTEGER,
            email          TEXT UNIQUE,
            phone_number   TEXT UNIQUE,
            phone_verified INTEGER DEFAULT 1,
            google_id      TEXT UNIQUE,
            token_version  INTEGER NOT NULL DEFAULT 1,
            created_at     TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")

    conn.commit()
    conn.close()

# ── MSG91 Widget Access-Token Verification ──────────────────────────────────────
# The client-side widget (sodhani-web) sends and verifies the OTP itself via
# MSG91's JS SDK. This is the one server-side check left: confirming the
# resulting access token is genuine before trusting it as proof of phone
# ownership, since a client-supplied token can't otherwise be trusted.

def normalize_phone(phone: str) -> str:
    clean = re.sub(r"\D", "", phone or "")
    if len(clean) == 10:
        return "91" + clean
    return clean

def verify_msg91_widget_access_token(access_token: str) -> bool:
    if not MSG91_AUTH_KEY:
        logger.error("Missing MSG91_AUTH_KEY in .env!")
        return False

    url = "https://control.msg91.com/api/v5/widget/verifyAccessToken"
    payload = json.dumps({
        "authkey": MSG91_AUTH_KEY,
        "access-token": access_token
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "accept": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            logger.debug("MSG91 verifyAccessToken response: type=%s status=%s", data.get("type"), data.get("status"))
            return data.get("type") == "success" or data.get("status") == "success"
    except urllib.error.HTTPError as e:
        logger.error("MSG91 verifyAccessToken HTTP error %s", e.code)
        return False
    except Exception as e:
        logger.error("MSG91 verifyAccessToken error: %s", e)
        return False

# ── Scheduler ────────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler()

def scheduled_fetch():
    """Background job to fetch announcements."""
    today = datetime.today().strftime("%Y%m%d")
    print(f"[Scheduler] Fetching announcements for {today}...")
    try:
        count = update(from_date=today, to_date=today)
        print(f"[Scheduler] Fetched {count} new announcements")
    except Exception as e:
        print(f"[Scheduler] Error fetching: {e}")

# ── App ──────────────────────────────────────────────────────────────────────────

app = FastAPI(title="BSE Announcements & Equity API")

@app.on_event("startup")
def on_startup():
    init_db()

    scheduler.add_job(
        scheduled_fetch,
        IntervalTrigger(hours=15),
        id="fetch_periodic",
        replace_existing=True,
    )

    scheduler.start()
    print("[Startup] Scheduler started - fetching every 15 hours")
    scheduled_fetch()

@app.on_event("shutdown")
def on_shutdown():
    scheduler.shutdown()

# Local dev origins are always trusted; production frontend origin(s) come from
# FRONTEND_URL (comma-separated for multiple). No wildcard fallback — an unset
# FRONTEND_URL in production should mean "no extra origins", not "trust everyone".
allowed_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]
for origin in os.environ.get("FRONTEND_URL", "").split(","):
    origin = origin.strip()
    if not origin:
        continue
    allowed_origins.append(origin)
    if origin.endswith("/"):
        allowed_origins.append(origin[:-1])

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Models ───────────────────────────────────────────────────────────────────────

class SendOtpRequest(BaseModel):
    phone_number: str
    flow: str | None = "any"  # "login", "signup", or "any"

class VerifySignupRequest(BaseModel):
    phone_number: str
    access_token: str
    name: str
    age: int = Field(gt=0, lt=150)
    email: EmailStr | None = None

class VerifyLoginRequest(BaseModel):
    phone_number: str
    access_token: str

class GoogleAuthRequest(BaseModel):
    credential: str
    email: EmailStr | None = None
    name: str | None = None

class AuthResponse(BaseModel):
    token: str
    user: dict

class AnnouncementResponse(BaseModel):
    total: int
    announcements: list[dict]

class ResearchReportsResponse(BaseModel):
    source: str
    status_code: int | None
    run_date: str
    start_date: str
    end_date: str
    days: int
    total_reports: int
    reports_count: int
    reports: list[dict]

# ── Helpers ──────────────────────────────────────────────────────────────────────

def _query(scrip_cd: str, limit: int, offset: int) -> tuple[list[dict], int]:
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row

    total = conn.execute(
        "SELECT COUNT(*) FROM announcements WHERE scrip_cd = ?",
        (scrip_cd,),
    ).fetchone()[0]

    rows = conn.execute(
        "SELECT * FROM announcements WHERE scrip_cd = ? ORDER BY news_dt DESC LIMIT ? OFFSET ?",
        (scrip_cd, limit, offset),
    ).fetchall()

    conn.close()
    return [dict(r) for r in rows], total

def _get_research_reports(days: int) -> dict:
    now = datetime.now(timezone.utc)
    cache_key = (days,)
    cached = _research_reports_cache.get(cache_key)

    if cached:
        cached_at, payload = cached
        if now - cached_at < timedelta(seconds=RESEARCH_REPORT_CACHE_TTL_SECONDS):
            return payload

    payload = build_reports_payload(days=days)
    _research_reports_cache[cache_key] = (now, payload)
    return payload

# ── Public & Auth Routes ────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/api/auth/check-phone")
def check_phone(body: SendOtpRequest):
    """Format-only pre-flight, kept for API compatibility with the client-side
    Widget-JS OTP flow (which sends and verifies the OTP itself). Deliberately
    does not reveal whether the phone number has an account — that check is
    deferred to verify-otp-signup / verify-otp-login, where it takes a real
    completed OTP to reach."""
    phone = body.phone_number.strip()

    if not phone or len(phone) < 10:
        raise HTTPException(status_code=400, detail="Please enter a valid phone number")

    return {"ok": True}

@app.post("/api/auth/verify-otp-signup", response_model=AuthResponse)
def verify_otp_signup(body: VerifySignupRequest):
    phone = body.phone_number.strip()
    clean_phone = normalize_phone(phone)
    access_token = body.access_token.strip()

    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Name is required")

    if not access_token or not verify_msg91_widget_access_token(access_token):
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row

    existing = conn.execute(
        "SELECT id FROM users WHERE phone_number = ? OR phone_number = ?",
        (phone, clean_phone)
    ).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="An account with this phone number already exists. Please log in instead.")

    user_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    clean_email = body.email.strip().lower() if body.email and body.email.strip() else None

    if clean_email:
        existing_email = conn.execute(
            "SELECT id FROM users WHERE email = ?", (clean_email,)
        ).fetchone()
        if existing_email:
            conn.close()
            raise HTTPException(status_code=400, detail="An account with this email already exists.")

    try:
        conn.execute(
            """INSERT INTO users (id, name, age, email, phone_number, phone_verified, created_at)
               VALUES (?, ?, ?, ?, ?, 1, ?)""",
            (user_id, body.name.strip(), body.age, clean_email, clean_phone, now_iso),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="An account with this phone number or email already exists.")
    conn.close()

    user_payload = {
        "id": user_id,
        "name": body.name.strip(),
        "age": body.age,
        "email": clean_email,
        "phone_number": clean_phone,
        "created_at": now_iso,
    }
    token = create_access_token(user_id, clean_phone)
    return {"token": token, "user": user_payload}

@app.post("/api/auth/verify-otp-login", response_model=AuthResponse)
def verify_otp_login(body: VerifyLoginRequest):
    phone = body.phone_number.strip()
    clean_phone = normalize_phone(phone)
    access_token = body.access_token.strip()

    if not access_token or not verify_msg91_widget_access_token(access_token):
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    user_row = conn.execute(
        "SELECT * FROM users WHERE phone_number = ? OR phone_number = ?", 
        (phone, clean_phone)
    ).fetchone()
    conn.close()

    if not user_row:
        raise HTTPException(status_code=404, detail="No account found with this phone number. Please sign up.")

    user_dict = dict(user_row)
    token = create_access_token(user_dict["id"], user_dict["phone_number"], user_dict["token_version"])
    del user_dict["token_version"]
    return {"token": token, "user": user_dict}

@app.post("/api/auth/google", response_model=AuthResponse)
def google_auth(body: GoogleAuthRequest):
    if not _google_client_id_configured():
        raise HTTPException(
            status_code=500,
            detail="Google sign-in is not configured on the server (GOOGLE_CLIENT_ID missing)."
        )

    try:
        claims = google_id_token.verify_oauth2_token(
            body.credential, google_auth_requests.Request(), GOOGLE_CLIENT_ID
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid Google credential: {exc}") from exc

    if not claims.get("email_verified", False):
        raise HTTPException(status_code=400, detail="Google account email is not verified")

    email = (claims.get("email") or body.email or "").strip().lower()
    name = body.name or claims.get("name") or (email.split("@")[0].capitalize() if email else "User")

    if "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid Google authentication credential")

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    user_row = conn.execute("SELECT * FROM users WHERE email = ? OR google_id = ?", (email, email)).fetchone()

    if user_row:
        user_id = user_row["id"]
        user_dict = dict(user_row)
        conn.close()
        token_version = user_dict.pop("token_version")
    else:
        user_id = str(uuid.uuid4())
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO users (id, name, age, email, phone_number, phone_verified, google_id, created_at)
               VALUES (?, ?, ?, ?, NULL, 0, ?, ?)""",
            (user_id, name, 25, email, email, now_iso),
        )
        conn.commit()
        conn.close()
        token_version = 1
        user_dict = {
            "id": user_id,
            "name": name,
            "age": 25,
            "email": email,
            "phone_number": None,
            "created_at": now_iso,
        }

    token = create_access_token(user_id, email, token_version)
    return {"token": token, "user": user_dict}

@app.get("/api/auth/me")
def get_me(current_user: dict = Depends(get_current_user)):
    return current_user

@app.post("/api/auth/logout")
def logout(current_user: dict = Depends(get_current_user)):
    """Bumps token_version so the JWT just used (and any other outstanding
    tokens for this user) fail validation in get_current_user from now on."""
    conn = get_db_connection()
    conn.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (current_user["id"],))
    conn.commit()
    conn.close()
    return {"ok": True}

# ── Protected Data Routes ────────────────────────────────────────────────────────

@app.get("/api/announcements")
def recent_announcements(
    limit: int = Query(default=5, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM announcements ORDER BY news_dt DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return {"announcements": [dict(r) for r in rows]}

@app.get("/api/announcements/{scrip_cd}", response_model=AnnouncementResponse)
def announcements_by_scrip(
    scrip_cd: str,
    limit: int = Query(default=5, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
):
    announcements, total = _query(scrip_cd, limit, offset)
    return {"total": total, "announcements": announcements}

@app.get("/api/research-reports", response_model=ResearchReportsResponse)
def research_reports(
    days: int = Query(default=15, ge=0, le=90),
    current_user: dict = Depends(get_current_user),
):
    try:
        return _get_research_reports(days)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not fetch Business Standard research reports: {exc}",
        ) from exc

def _equity_file_response(file_name: str, output_dir: str) -> FileResponse:
    clean_name = os.path.basename(file_name)
    if clean_name != file_name or clean_name in {"", ".", ".."}:
        raise HTTPException(status_code=404, detail="Equity file not found")

    base_name = clean_name[:-5] if clean_name.lower().endswith(".json") else clean_name
    file_path = os.path.join(output_dir, f"{base_name}.json")

    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Equity file not found")

    return FileResponse(file_path, media_type="application/json")

def _equity_output_dir(consolidated: bool) -> str:
    output_dir = OUTPUT_CONSOLIDATED_DIR if consolidated else OUTPUT_DIR
    nested_dir = os.path.join(output_dir, os.path.basename(output_dir))
    if os.path.isdir(nested_dir):
        return nested_dir
    return output_dir

@app.get("/api/equity")
def equity_json_by_code(
    code: str = Query(..., min_length=1),
    consolidated: bool = Query(default=False),
    current_user: dict | None = Depends(get_optional_user),
):
    return _equity_file_response(code, _equity_output_dir(consolidated))

@app.post("/admin/fetch")
def admin_fetch():
    """Trigger BSE announcement fetch (run once to populate DB)."""
    today = datetime.today().strftime("%Y%m%d")
    count = update(from_date=today, to_date=today)
    return {"fetched": count, "date": today}

