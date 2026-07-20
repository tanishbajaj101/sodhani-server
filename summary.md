## Key Components

### 1. `announcement.py` - Scraper Core

- Fetches announcements from BSE India API
- Incremental updates using `last_newsid` tracker
- Stores full JSON in SQLite with indexing by `scrip_cd`
- Duplicate prevention via `INSERT OR IGNORE`

### 2. `server.py` - FastAPI Server

- **Background Scheduler:** APScheduler with cron triggers
- **Market Hours Only:** 9:15 AM - 3:30 PM IST (3:45-10:00 AM UTC)
- **Frequency:** Every 5 minutes during market hours
- **Zero compute outside hours** (no interval polling)

**Endpoints:**

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Liveness check |
| `GET /api/announcements?limit=5` | Recent announcements |
| `GET /api/announcements/{scrip_cd}?limit=5&offset=0` | By BSE security code |
| `POST /admin/fetch` | Manual trigger (one-time) |

### 3. Database Schema

```sql
CREATE TABLE announcements (
    newsid     TEXT PRIMARY KEY,
    scrip_cd   TEXT,
    news_dt    TEXT,
    raw_json   TEXT,
    fetched_at TEXT
);

-- Indexes for fast queries
CREATE INDEX idx_scrip_cd ON announcements(scrip_cd);
CREATE INDEX idx_news_dt ON announcements(news_dt);
CREATE INDEX idx_fetched_at ON announcements(fetched_at);
```

## Deployment

**Platform:** Railway.app  
**Runtime:** Python 3.13  
**Database:** SQLite (persistent volume)  
**URL:** `https://server-production-8226.up.railway.app`

### Infrastructure

- **Volume:** `/data` mounted for SQLite persistence
- **Cron Schedule:** Only runs during BSE market hours
- **Region:** Asia Southeast (Singapore)

## Scheduler Behavior

```
Market Hours (IST):     9:15 AM ───────────────────────────────► 3:30 PM
                        │                                           │
UTC Cron Schedule:      3:45 AM ──► 4:00 ──► 4:05 ... 9:55 ──► 10:00
                        ◄────────── Every 5 minutes ──────────────►

Outside Hours:          NO JOBS RUN (zero compute)
```

## File Structure

```
c:\server\
├── announcement.py      # BSE scraper logic
├── server.py            # FastAPI + scheduler
├── seed.py              # One-time DB population (optional)
├── requirements.txt     # Python deps
├── pyproject.toml       # Project metadata
├── Procfile             # Railway process definition
├── bse_announcements.db # SQLite database (volume)
├── last_newsid.json     # Tracker for incremental updates
├── railway-works.md     # Deployment commands
└── summary.md           # This file
```

## Key Features

1. **Incremental Updates:** Only fetches new announcements since last run
2. **Idempotent:** Safe to run multiple times (duplicate prevention)
3. **Efficient:** Zero wasted compute outside market hours
4. **Fast Queries:** Indexed by scrip code for O(1) lookups
5. **Full Data:** Stores complete JSON response (no field loss)

## Usage Examples

```bash
# Get recent announcements
curl https://server-production-8226.up.railway.app/api/announcements

# Get announcements for Reliance (scrip: 500325)
curl "https://server-production-8226.up.railway.app/api/announcements/500325?limit=5"

# Check health
curl https://server-production-8226.up.railway.app/health
```

## Technical Decisions

| Decision | Rationale |
|----------|-----------|
| SQLite vs Postgres | Single container, simple, zero config |
| APScheduler cron | No wasted compute vs interval polling |
| IST market hours | BSE only releases announcements during trading |
| Full JSON storage | Future-proof, no schema migrations |
| NEWSID tracking | BSE's unique ID for incremental sync |
| Volume at /data | Railway's persistent storage convention |
