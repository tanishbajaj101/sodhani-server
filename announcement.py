"""
BSE India Announcements — SQLite-backed incremental updater.
Fetches announcements until a previously-seen NEWSID is hit, then stops.
"""

import json
import os
import sqlite3
import time
from datetime import datetime
from typing import Optional

import requests

# ── Config ──────────────────────────────────────────────────────────────────────

BASE_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DATA_DIR, "bse_announcements.db")
TRACKER_PATH = os.path.join(DATA_DIR, "last_newsid.json")

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.bseindia.com",
    "Referer": "https://www.bseindia.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


# ── Database ────────────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS announcements (
            newsid     TEXT PRIMARY KEY,
            scrip_cd   TEXT,
            news_dt    TEXT,
            raw_json   TEXT,
            fetched_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scrip_cd ON announcements(scrip_cd)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_dt ON announcements(news_dt)")
    conn.commit()
    return conn


def insert_record(conn: sqlite3.Connection, record: dict) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO announcements (newsid, scrip_cd, news_dt, raw_json, fetched_at) VALUES (?, ?, ?, ?, ?)",
        (
            record.get("NEWSID"),
            record.get("SCRIP_CD"),
            record.get("NEWS_DT"),
            json.dumps(record, ensure_ascii=False),
            datetime.utcnow().isoformat(),
        ),
    )


# ── Tracker ─────────────────────────────────────────────────────────────────────

def read_last_newsid() -> Optional[str]:
    if not os.path.exists(TRACKER_PATH):
        return None
    with open(TRACKER_PATH, "r", encoding="utf-8") as f:
        return json.load(f).get("last_newsid")


def write_last_newsid(newsid: str) -> None:
    with open(TRACKER_PATH, "w", encoding="utf-8") as f:
        json.dump({"last_newsid": newsid}, f, indent=2)


# ── Fetch ───────────────────────────────────────────────────────────────────────

def fetch_page(
    from_date: str,
    to_date: str,
    page: int,
    session: requests.Session,
    scrip: str = "",
    category: str = "-1",
    subcategory: str = "-1",
    ann_type: str = "C",
) -> list[dict]:
    params = {
        "pageno":      page,
        "strCat":      category,
        "strPrevDate": from_date,
        "strScrip":    scrip,
        "strSearch":   "P",
        "strToDate":   to_date,
        "strType":     ann_type,
        "subcategory": subcategory,
    }
    resp = session.get(BASE_URL, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json().get("Table", [])


# ── Update Flow ─────────────────────────────────────────────────────────────────

def update(
    from_date: str,
    to_date: str,
    scrip: str = "",
    category: str = "-1",
    ann_type: str = "C",
    delay: float = 0.5,
    max_pages: int = 50,
) -> int:
    """Fetch new announcements until we hit the last-seen NEWSID. Returns count inserted."""
    last_newsid = read_last_newsid()
    conn = init_db()
    newer_newsid: Optional[str] = None
    inserted = 0

    with requests.Session() as session:
        for page in range(1, max_pages + 1):
            records = fetch_page(from_date, to_date, page, session, scrip=scrip, category=category, ann_type=ann_type)
            print(f"  Page {page}: {len(records)} records", flush=True)

            if not records:
                break

            # Capture the first record of page 1 as the new high-water mark
            if newer_newsid is None:
                newer_newsid = records[0].get("NEWSID")

            seen_last = False
            for rec in records:
                if rec.get("NEWSID") == last_newsid:
                    seen_last = True
                    break
                insert_record(conn, rec)
                inserted += 1

            conn.commit()

            if seen_last:
                print(f"  Hit last_newsid — caught up.", flush=True)
                break

            time.sleep(delay)

    # Advance tracker to the newest announcement we've now processed
    if newer_newsid:
        write_last_newsid(newer_newsid)

    conn.close()
    return inserted


# ── Query Helper ────────────────────────────────────────────────────────────────

def get_by_scrip(scrip_cd: str) -> list[dict]:
    """Return all announcements for a given BSE security code (newest first)."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT raw_json FROM announcements WHERE scrip_cd = ? ORDER BY news_dt DESC",
        (scrip_cd,),
    ).fetchall()
    conn.close()
    return [json.loads(r[0]) for r in rows]


def get_recent(limit: int = 20) -> list[dict]:
    """Return the most recently-fetched announcements."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT raw_json FROM announcements ORDER BY fetched_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [json.loads(r[0]) for r in rows]


# ── CLI ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    TODAY = datetime.today().strftime("%Y%m%d")

    print("=" * 50)
    print("BSE Announcements — Incremental Update")
    print("=" * 50)

    print(f"\n[Updating] {TODAY}")
    count = update(from_date=TODAY, to_date=TODAY)
    print(f"\nDone — {count} new announcements inserted.")
