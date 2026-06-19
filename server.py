"""
BSE Announcements API server.
"""

import os
import sqlite3
from datetime import datetime, timedelta, time

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from announcement import update
from get_business_standard_response import build_reports_payload

# ── Paths ────────────────────────────────────────────────────────────────────────

DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DATA_DIR, "bse_announcements.db")
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
OUTPUT_CONSOLIDATED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output_consolidated")
RESEARCH_REPORT_CACHE_TTL_SECONDS = int(os.environ.get("RESEARCH_REPORT_CACHE_TTL_SECONDS", "21600"))
_research_reports_cache: dict[tuple[int], tuple[datetime, dict]] = {}

# ── DB Init ─────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Ensure the announcements table exists."""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)

    # Migrate from old schema if needed (drop old table, recreate with new columns)
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
    conn.commit()
    conn.close()

# ── Scheduler ────────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler()

# BSE Market hours: 9:15 AM - 3:30 PM IST
# Converted to UTC: 3:45 AM - 10:00 AM UTC (IST = UTC+5:30)

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

app = FastAPI(title="BSE Announcements API")

@app.on_event("startup")
def on_startup():
    init_db()
    
    # Cron schedule: Only run during BSE market hours (9:15 AM - 3:30 PM IST = 3:45-10:00 UTC)
    # Every 5 minutes: at :00, :05, :10, :15, :20, :25, :30, :35, :40, :45, :50, :55
    scheduler.add_job(
        scheduled_fetch,
        "cron",
        hour="3-9",      # 3:45 AM UTC to 9:55 AM UTC
        minute="45-59/5,*/5",  # 3:45, 3:50, 3:55, then every 5 min until 9:55
        id="fetch_morning",
        replace_existing=True,
    )
    # 10:00 AM UTC (3:30 PM IST exactly)
    scheduler.add_job(
        scheduled_fetch,
        "cron",
        hour="10",
        minute="0",
        id="fetch_close",
        replace_existing=True,
    )
    
    scheduler.start()
    print("[Startup] Scheduler started - fetching every 5 minutes during market hours ONLY (9:15 AM - 3:30 PM IST)")
    print("[Startup] Zero compute outside market hours")
    # Initial fetch
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


# ── Routes ───────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/announcements")
def recent_announcements(limit: int = Query(default=5, ge=1, le=100)):
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
):
    announcements, total = _query(scrip_cd, limit, offset)
    return {"total": total, "announcements": announcements}


@app.get("/api/research-reports", response_model=ResearchReportsResponse)
def research_reports(days: int = Query(default=15, ge=0, le=90)):
    try:
        return _get_research_reports(days)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not fetch Business Standard research reports: {exc}",
        ) from exc


# ── Admin ───────────────────────────────────────────────────────────────────────

# Equity static JSON

def _equity_file_response(file_name: str, output_dir: str) -> FileResponse:
    clean_name = os.path.basename(file_name)
    if clean_name != file_name or clean_name in {"", ".", ".."}:
        raise HTTPException(status_code=404, detail="Equity file not found")

    base_name = clean_name[:-5] if clean_name.lower().endswith(".json") else clean_name
    file_path = os.path.join(output_dir, f"{base_name}.json")

    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Equity file not found")

    return FileResponse(file_path, media_type="application/json")


@app.get("/equity/{file_name}/consolidated")
def equity_consolidated_json(file_name: str):
    return _equity_file_response(file_name, OUTPUT_CONSOLIDATED_DIR)


@app.get("/equity/{file_name}")
def equity_json(file_name: str):
    return _equity_file_response(file_name, OUTPUT_DIR)


@app.post("/admin/fetch")
def admin_fetch():
    """Trigger BSE announcement fetch (run once to populate DB)."""
    today = datetime.today().strftime("%Y%m%d")
    count = update(from_date=today, to_date=today)
    return {"fetched": count, "date": today}
