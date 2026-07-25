"""
BSE Announcements & Equity API server with User Authentication.
"""

import os
import sqlite3
import uuid
from datetime import datetime, timedelta, time

import jwt
import bcrypt
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException, Query, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from announcement import update
from get_business_standard_response import build_reports_payload

# ── Security & Auth Config ────────────────────────────────────────────────────────
JWT_SECRET = os.environ.get("JWT_SECRET", "sodhani-safeedge-jwt-secret-key-2026")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24 * 7  # 7 days

security_scheme = HTTPBearer(auto_error=False)

def hash_password(password: str) -> str:
    pw_bytes = password.encode("utf-8")[:72]
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(pw_bytes, salt).decode("utf-8")

def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        pw_bytes = plain_password.encode("utf-8")[:72]
        hash_bytes = password_hash.encode("utf-8")
        return bcrypt.checkpw(pw_bytes, hash_bytes)
    except Exception:
        return False

def create_access_token(user_id: str, email: str) -> str:
    expires = datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS)
    payload = {
        "sub": user_id,
        "email": email,
        "exp": expires
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

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
        email: str = payload.get("email")
        if not user_id or not email:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return {"id": user_id, "email": email}
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
        email: str = payload.get("email")
        if not user_id or not email:
            return None
        return {"id": user_id, "email": email}
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

def init_db() -> None:
    """Ensure the announcements and users tables exist."""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)

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

    # Users Table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            TEXT PRIMARY KEY,
            email         TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at    TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")

    conn.commit()
    conn.close()

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Models ───────────────────────────────────────────────────────────────────────

class SignupRequest(BaseModel):
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

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
    conn = sqlite3.connect(DB_PATH)
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
    now = datetime.utcnow()
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

@app.post("/api/auth/signup", response_model=AuthResponse)
def signup(body: SignupRequest):
    email = body.email.strip().lower()
    password = body.password.strip()

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email address")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters long")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="An account with this email already exists")

    user_id = str(uuid.uuid4())
    pw_hash = hash_password(password)
    now_iso = datetime.utcnow().isoformat()

    conn.execute(
        "INSERT INTO users (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (user_id, email, pw_hash, now_iso),
    )
    conn.commit()
    conn.close()

    token = create_access_token(user_id, email)
    return {"token": token, "user": {"id": user_id, "email": email}}

@app.post("/api/auth/login", response_model=AuthResponse)
def login(body: LoginRequest):
    email = body.email.strip().lower()
    password = body.password.strip()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    user_row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()

    if not user_row or not verify_password(password, user_row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    user_id = user_row["id"]
    token = create_access_token(user_id, email)
    return {"token": token, "user": {"id": user_id, "email": email}}

@app.get("/api/auth/me")
def get_me(current_user: dict = Depends(get_current_user)):
    return current_user

# ── Protected Data Routes ────────────────────────────────────────────────────────

@app.get("/api/announcements")
def recent_announcements(
    limit: int = Query(default=5, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    conn = sqlite3.connect(DB_PATH)
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

