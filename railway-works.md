# Railway Deployment Guide

This document lists the Railway CLI commands that actually worked for deploying the BSE Announcements API server.

## Deployment Steps

### 1. Link to Existing Project

```bash
railway link --project server
```
Select workspace → select project → select environment (production)

### 2. Link to Service (within project)

```bash
railway service link server
```

### 3. Check Status

```bash
railway status
railway service status
railway service list
```

### 4. Deploy

```bash
railway up --detach
```

### 5. View Logs

```bash
railway service logs --tail 30
railway logs --tail 50
```

### 6. Volume Management

Create a persistent volume for SQLite:

```bash
railway volume add --mount-path /data
```

Verify volume:

```bash
railway volume list
```

### 7. Environment Variables

Set:

```bash
railway variable set DATA_DIR=/data
```

List all:

```bash
railway variable list
```

### 8. Run One-off Commands

Execute Python in the Railway environment:

```bash
railway run python script.py
```

### 9. Open Dashboard

```bash
railway open
```

## Working Configuration

**Project:** `server`  
**Service:** `server`  
**Region:** `asia-southeast1-eqsg3a`  
**Volume:** Mounted at `/data`  
**Env Var:** `DATA_DIR=/data`

## Files for Railway

- `Procfile` - Defines web process and release command
- `requirements.txt` - Python dependencies
- `pyproject.toml` - Alternative dependency specification
- `server.py` - FastAPI application entry point

## Important Notes

1. **Volume persistence:** SQLite DB lives in `/data/` which is the mounted volume
2. **No SSH needed:** Use `railway run` for one-off commands instead of SSH
3. **Auto-deploy:** `railway up` builds and deploys in one step
4. **Logs:** Check logs immediately after deploy to verify startup
