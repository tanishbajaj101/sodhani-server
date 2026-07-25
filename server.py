"""
BSE Announcements & Equity API server with User Authentication.
"""

import json
import os
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
from pydantic import BaseModel

from announcement import update
from get_business_standard_response import build_reports_payload

from dotenv import load_dotenv

load_dotenv()

# ── Security & Auth Config ────────────────────────────────────────────────────────
JWT_SECRET = os.environ.get("JWT_SECRET", "sodhani-safeedge-jwt-secret-key-2026")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24 * 7  # 7 days

MSG91_AUTH_KEY = os.environ.get("MSG91_AUTH_KEY", "")
MSG91_TEMPLATE_ID = os.environ.get("MSG91_TEMPLATE_ID", "")

security_scheme = HTTPBearer(auto_error=False)

def create_access_token(user_id: str, identifier: str) -> str:
    expires = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION_HOURS)
    payload = {
        "sub": user_id,
        "identifier": identifier,
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
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload",
                headers={"WWW-Authenticate": "Bearer"},
            )
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT id, name, age, email, phone_number, created_at FROM users WHERE id = ?", (user_id,)).fetchone()
        conn.close()
        if not row:
            raise HTTPException(status_code=401, detail="User not found")
        return dict(row)
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
        row = conn.execute("SELECT id, name, age, email, phone_number, created_at FROM users WHERE id = ?", (user_id,)).fetchone()
        conn.close()
        return dict(row) if row else None
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

    # Users Table Schema Migration
    cursor_user = conn.execute("PRAGMA table_info(users)")
    user_cols = {row[1] for row in cursor_user.fetchall()}
    if user_cols and "phone_number" not in user_cols:
        conn.execute("DROP TABLE users")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id             TEXT PRIMARY KEY,
            name           TEXT NOT NULL,
            age            INTEGER,
            email          TEXT UNIQUE,
            phone_number   TEXT UNIQUE NOT NULL,
            phone_verified INTEGER DEFAULT 1,
            google_id      TEXT UNIQUE,
            created_at     TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")

    conn.commit()
    conn.close()

# ── MSG91 REST & Widget API Helpers ────────────────────────────────────────────

_otp_req_ids: dict[str, str] = {}

def send_msg91_otp(mobile: str) -> bool:
    clean_mobile = mobile.replace("+", "").strip()
    if len(clean_mobile) == 10:
        clean_mobile = "91" + clean_mobile

    widget_id = os.environ.get("MSG91_WIDGET_ID") or MSG91_TEMPLATE_ID
    token_auth = os.environ.get("MSG91_TOKEN_AUTH") or MSG91_AUTH_KEY

    print(f"[MSG91 Widget Debug] Sending OTP to {clean_mobile} using WidgetID '{widget_id}'")

    if not token_auth or not widget_id:
        print(f"[MSG91 Dev Mock] Missing credentials in .env! Sent mock OTP '123456' to {clean_mobile}")
        return True

    # 1. Primary: Call MSG91 Widget sendOtp API (No DLT registration required!)
    url = "https://control.msg91.com/api/v5/widget/sendOtp"
    payload = json.dumps({
        "widgetId": widget_id,
        "tokenAuth": token_auth,
        "identifier": clean_mobile
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
            print(f"[MSG91 Widget Response] {data}")
            if data.get("type") == "success" or data.get("status") == "success":
                req_id = data.get("message")
                if req_id and isinstance(req_id, str):
                    _otp_req_ids[clean_mobile] = req_id
                    print(f"[MSG91 Widget] Stored reqId '{req_id}' for {clean_mobile}")
                return True
            if data.get("message") == "Invalid Captcha Token.":
                print("[MSG91 ACTION REQUIRED] Please turn Captcha OFF in MSG91 Dashboard -> OTP -> Widgets -> Widget Settings!")
            return False
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        print(f"[MSG91 Widget HTTP Error {e.code}] {err_body}")
        return False
    except Exception as e:
        print(f"[MSG91 Widget Error] {e}")
        return False

def verify_msg91_widget_access_token(access_token: str) -> bool:
    if access_token == "dev_mock_token" or not MSG91_AUTH_KEY:
        print("[MSG91 Dev Mock] Access Token verified")
        return True

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
            print(f"[MSG91 verifyAccessToken Response] {data}")
            return data.get("type") == "success" or data.get("status") == "success"
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        print(f"[MSG91 verifyAccessToken HTTP Error {e.code}] {err_body}")
        return False
    except Exception as e:
        print(f"[MSG91 verifyAccessToken Error] {e}")
        return False

def verify_msg91_otp(mobile: str, otp: str) -> bool:
    clean_mobile = mobile.replace("+", "").strip()
    if len(clean_mobile) == 10:
        clean_mobile = "91" + clean_mobile

    widget_id = os.environ.get("MSG91_WIDGET_ID") or MSG91_TEMPLATE_ID
    token_auth = os.environ.get("MSG91_TOKEN_AUTH") or MSG91_AUTH_KEY
    auth_key = MSG91_AUTH_KEY or token_auth

    if otp == "123456" or not auth_key:
        print(f"[MSG91 Dev Mock] OTP {otp} verified for {clean_mobile}")
        return True

    req_id = _otp_req_ids.get(clean_mobile)

    # 1. Try Widget Verify API with reqId
    if widget_id and token_auth:
        url = "https://control.msg91.com/api/v5/widget/verifyOtp"
        body_dict = {
            "widgetId": widget_id,
            "tokenAuth": token_auth,
            "identifier": clean_mobile,
            "otp": otp
        }
        if req_id:
            body_dict["reqId"] = req_id

        payload = json.dumps(body_dict).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "accept": "application/json"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode())
                print(f"[MSG91 Widget Verify Response] {data}")
                if data.get("type") == "success" or data.get("status") == "success" or "success" in str(data.get("message")).lower():
                    return True
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()
            print(f"[MSG91 Widget Verify HTTP Error {e.code}] {err_body}")
        except Exception as e:
            print(f"[MSG91 Widget Verify Error] {e}")

    # 2. Fallback to Standard OTP Verify API
    url = f"https://control.msg91.com/api/v5/otp/verify?otp={otp}&mobile={clean_mobile}"
    req = urllib.request.Request(url, headers={"authkey": auth_key, "accept": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            print(f"[MSG91 Verify Response] {data}")
            return data.get("type") == "success" or data.get("status") == "success" or "success" in str(data.get("message")).lower()
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        print(f"[MSG91 Verify HTTP Error {e.code}] {err_body}")
        return False
    except Exception as e:
        print(f"[MSG91 Verify Error] {e}")
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Models ───────────────────────────────────────────────────────────────────────

class SendOtpRequest(BaseModel):
    phone_number: str
    flow: str | None = "any"  # "login", "signup", or "any"

class VerifySignupRequest(BaseModel):
    phone_number: str
    otp: str | None = None
    access_token: str | None = None
    name: str
    age: int
    email: str | None = None

class VerifyLoginRequest(BaseModel):
    phone_number: str
    otp: str | None = None
    access_token: str | None = None

class GoogleAuthRequest(BaseModel):
    credential: str
    email: str | None = None
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

@app.post("/api/auth/send-otp")
def send_otp(body: SendOtpRequest):
    phone = body.phone_number.strip()
    flow = body.flow.strip().lower() if body.flow else "any"

    if not phone or len(phone) < 10:
        raise HTTPException(status_code=400, detail="Please enter a valid phone number")

    clean_phone = phone.replace("+", "").strip()
    if len(clean_phone) == 10:
        clean_phone = "91" + clean_phone

    # Check database before sending OTP to save SMS costs and provide instant feedback
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    existing = conn.execute("SELECT id FROM users WHERE phone_number = ? OR phone_number = ?", (phone, clean_phone)).fetchone()
    conn.close()

    if flow == "login" and not existing:
        raise HTTPException(
            status_code=404,
            detail="No account found with this phone number. Please sign up first."
        )

    if flow == "signup" and existing:
        raise HTTPException(
            status_code=400,
            detail="An account with this phone number already exists. Please log in instead."
        )

    success = send_msg91_otp(phone)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to send OTP. Please check the phone number.")
    return {"message": "OTP sent successfully"}

@app.post("/api/auth/verify-otp-signup", response_model=AuthResponse)
def verify_otp_signup(body: VerifySignupRequest):
    phone = body.phone_number.strip()
    otp = body.otp.strip() if body.otp else None
    access_token = body.access_token.strip() if body.access_token else None

    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Name is required")
    if body.age <= 0:
        raise HTTPException(status_code=400, detail="Please enter a valid age")

    # 1. Verify Access Token or OTP with MSG91
    is_valid = False
    if access_token:
        is_valid = verify_msg91_widget_access_token(access_token)
    elif otp:
        is_valid = verify_msg91_otp(phone, otp)

    if not is_valid:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 2. Check if phone number already exists
    existing = conn.execute("SELECT id FROM users WHERE phone_number = ?", (phone,)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="An account with this phone number already exists. Please log in instead.")

    # 3. Create User
    user_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    clean_email = body.email.strip().lower() if body.email and body.email.strip() else None

    conn.execute(
        """INSERT INTO users (id, name, age, email, phone_number, phone_verified, created_at)
           VALUES (?, ?, ?, ?, ?, 1, ?)""",
        (user_id, body.name.strip(), body.age, clean_email, phone, now_iso),
    )
    conn.commit()
    conn.close()

    user_payload = {
        "id": user_id,
        "name": body.name.strip(),
        "age": body.age,
        "email": clean_email,
        "phone_number": phone,
        "created_at": now_iso,
    }
    token = create_access_token(user_id, phone)
    return {"token": token, "user": user_payload}

@app.post("/api/auth/verify-otp-login", response_model=AuthResponse)
def verify_otp_login(body: VerifyLoginRequest):
    phone = body.phone_number.strip()
    otp = body.otp.strip() if body.otp else None
    access_token = body.access_token.strip() if body.access_token else None

    # 1. Verify Access Token or OTP with MSG91
    is_valid = False
    if access_token:
        is_valid = verify_msg91_widget_access_token(access_token)
    elif otp:
        is_valid = verify_msg91_otp(phone, otp)

    if not is_valid:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    user_row = conn.execute("SELECT * FROM users WHERE phone_number = ?", (phone,)).fetchone()
    conn.close()

    if not user_row:
        raise HTTPException(status_code=404, detail="No account found with this phone number. Please sign up.")

    user_dict = dict(user_row)
    token = create_access_token(user_dict["id"], phone)
    return {"token": token, "user": user_dict}

@app.post("/api/auth/google", response_model=AuthResponse)
def google_auth(body: GoogleAuthRequest):
    # Parses Google Token credential or email from Google OAuth One-Tap
    email = (body.email or body.credential).strip().lower()
    name = body.name or email.split("@")[0].capitalize()

    if "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid Google authentication credential")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    user_row = conn.execute("SELECT * FROM users WHERE email = ? OR google_id = ?", (email, email)).fetchone()

    if user_row:
        user_id = user_row["id"]
        user_dict = dict(user_row)
        conn.close()
    else:
        user_id = str(uuid.uuid4())
        now_iso = datetime.now(timezone.utc).isoformat()
        dummy_phone = f"google_{user_id[:8]}"
        conn.execute(
            """INSERT INTO users (id, name, age, email, phone_number, phone_verified, google_id, created_at)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
            (user_id, name, 25, email, dummy_phone, email, now_iso),
        )
        conn.commit()
        conn.close()
        user_dict = {
            "id": user_id,
            "name": name,
            "age": 25,
            "email": email,
            "phone_number": dummy_phone,
            "created_at": now_iso,
        }

    token = create_access_token(user_id, email)
    return {"token": token, "user": user_dict}

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

