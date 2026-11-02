"""FastAPI web app. Single-process scheduler; do not run multiple Uvicorn workers."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.sessions import SessionMiddleware

from .core import Engine, Store, now, safe_error, search_query, parse_resolution
from .settings import SettingsFile, RESOLUTIONS, NOTIFY

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
BASE = Path(__file__).parent
DATA = os.environ.get("DATA_DIR", "/data")
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SECRET = os.environ.get("APP_SECRET", "")
if len(PASSWORD) < 12 or len(SECRET) < 32 or PASSWORD.startswith("CHANGE_") or SECRET.startswith("CHANGE_"):
    raise RuntimeError("Set ADMIN_PASSWORD (at least 12 chars) and APP_SECRET (at least 32 chars) in .env")


settings = SettingsFile(str(Path(DATA) / "config.json"))
CONFIG = settings.data
store = Store(str(Path(DATA) / "nzbwatch.sqlite3"))
engine = Engine(store, CONFIG)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await engine.startup()
    yield
    await engine.shutdown()


app = FastAPI(title="NZBWatch", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET, session_cookie="nzbwatch_session", max_age=86400, https_only=os.getenv("COOKIE_SECURE", "false").lower() == "true", same_site="lax")

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    return response


# In-memory login throttle, per remote IP. For internet exposure also enforce throttling at the proxy.
failures: dict[str, list[float]] = {}


def require(request: Request, mutate: bool = False):
    if request.session.get("authenticated") is not True:
        raise HTTPException(401, "Login required")
    if mutate:
        csrf = request.session.get("csrf", "")
        token = request.headers.get("X-CSRF-Token", "")
        if not csrf or not hmac.compare_digest(csrf, token):
            raise HTTPException(403, "CSRF token missing or invalid")


class Credentials(BaseModel):
    password: str


class WatchIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    query: str = Field(min_length=1, max_length=250)
    enabled: bool = True
    mode: str = "auto"
    match_mode: str = "all"
    required_words: str = Field(default="", max_length=500)
    excluded_words: str = Field(default="", max_length=500)
    categories: str = Field(default="", max_length=120)
    sab_category: str = Field(default="", max_length=120)
    min_mb: float | None = Field(default=None, ge=0, le=10000000)
    max_mb: float | None = Field(default=None, ge=0, le=10000000)
    max_age_days: int | None = Field(default=None, ge=0, le=20000)
    max_grabs: int = Field(default=1, ge=0, le=10000)
    poll_minutes: int = Field(default=0, ge=0, le=10080)  # 0 uses global scan interval
    broad_search: bool = True
    resolution_mode: str = "inherit"

    @field_validator("name", "query")
    @classmethod
    def nonempty(cls, v):
        if not v.strip():
            raise ValueError("Cannot be blank")
        return v.strip()

    @field_validator("mode")
    @classmethod
    def valid_mode(cls, v):
        if v not in ("auto", "notify"):
            raise ValueError("Invalid mode")
        return v

    @field_validator("match_mode")
    @classmethod
    def valid_match(cls, v):
        if v not in ("all", "any", "phrase"):
            raise ValueError("Invalid match mode")
        return v

    @field_validator("resolution_mode")
    @classmethod
    def valid_resolution_mode(cls, v):
        if v not in ("inherit", "off", "on"):
            raise ValueError("Invalid resolution mode")
        return v

    @field_validator("poll_minutes")
    @classmethod
    def valid_interval(cls, v):
        if v != 0 and v < 5:
            raise ValueError("Interval must be 0 (global) or at least 5 minutes")
        return v

    @field_validator("categories")
    @classmethod
    def valid_categories(cls, v):
        if v and not all(x.strip().isdigit() for x in v.split(",")):
            raise ValueError("Categories must be numeric Newznab IDs separated by commas")
        return v


@app.get("/healthz")
async def health():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(BASE / "static" / "index.html", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/static/{filename}")
async def static(filename: str):
    if filename not in ("app.js", "style.css"):
        raise HTTPException(404)
    return FileResponse(BASE / "static" / filename, headers={"Cache-Control": "no-store"})


@app.post("/api/login")
async def login(request: Request, credentials: Credentials):
    ip = request.client.host if request.client else "unknown"
    t = time.monotonic()
    recent = [i for i in failures.get(ip, []) if t - i < 900]
    if len(recent) >= 7:
        raise HTTPException(429, "Too many login attempts. Try again in 15 minutes.")
    if not hmac.compare_digest(credentials.password.encode(), PASSWORD.encode()):
        failures[ip] = recent + [t]
        raise HTTPException(401, "Incorrect password")
    failures.pop(ip, None)
    request.session.clear()
    request.session.update({"authenticated": True, "csrf": secrets.token_urlsafe(32)})
    store.log("auth", "Administrator signed in")
    return {"ok": True, "csrf": request.session["csrf"]}


@app.get("/api/session")
async def session(request: Request):
    if request.session.get("authenticated") is not True:
        return {"authenticated": False}
    return {"authenticated": True, "csrf": request.session.get("csrf")}


@app.post("/api/logout")
async def logout(request: Request):
    require(request, True)
    request.session.clear()
    return {"ok": True}


@app.get("/api/state")
async def state(request: Request):
    require(request)
    return {"watches": store.watches(), **store.recent(),
            "preferences": {"watchlist_columns": CONFIG["watchlist_columns"]}, "integrations": {
        "hydra": bool(CONFIG["hydra_url"] and CONFIG["hydra_key"]),
        "sab": bool(CONFIG["sab_url"] and CONFIG["sab_key"]),
        "pushover": bool(CONFIG["pushover_user"] and CONFIG["pushover_key"]),
        "notify_events": sorted(CONFIG["notification_types"]),
    }, "time": now()}


class SettingsIn(BaseModel):
    hydra_url: str = Field(max_length=500)
    sab_url: str = Field(max_length=500)
    # null = keep existing; empty string = explicitly clear
    hydra_key: str | None = Field(default=None, max_length=512)
    sab_key: str | None = Field(default=None, max_length=512)
    pushover_key: str | None = Field(default=None, max_length=512)
    pushover_user: str | None = Field(default=None, max_length=512)
    notification_types: list[str]
    scan_interval_minutes: int = Field(ge=5, le=10080)
    max_queues_per_scan: int = Field(ge=1, le=20)
    clean_search_terms: bool
    strip_release_groups: bool
    resolution_enabled: bool
    resolution_order: list[str]
    fallback_min_resolution: str
    allow_unknown_resolution: bool
    dedupe_across_resolutions: bool
    watchlist_columns: int = Field(default=2, ge=1, le=2)

    @field_validator("notification_types")
    @classmethod
    def validate_notify(cls, v):
        if len(v) != len(set(v)) or any(x not in NOTIFY for x in v):
            raise ValueError("Invalid notification types")
        return v

    @field_validator("resolution_order")
    @classmethod
    def validate_resolutions(cls, v):
        if not v or len(v) != len(set(v)) or any(x not in RESOLUTIONS for x in v):
            raise ValueError("Resolution order must contain unique known resolutions")
        return v


@app.get("/api/settings")
async def get_settings(request: Request):
    require(request)
    return settings.public()


@app.put("/api/settings")
async def put_settings(request: Request, data: SettingsIn):
    require(request, True)
    submitted = data.model_dump()
    if engine.lock.locked():
        # Presentation-only changes can safely be saved during a scan.
        active_changes = {key for key, value in submitted.items()
                          if value is not None and value != CONFIG.get(key)}
        if active_changes - {"watchlist_columns"}:
            raise HTTPException(409, "Scan underway; save settings once the current scan finishes")
    old_interval = CONFIG["scan_interval_minutes"]
    try:
        result = settings.update(data.model_dump())
    except (ValueError, OSError) as ex:
        raise HTTPException(422, safe_error(ex))
    if old_interval != CONFIG["scan_interval_minutes"]:
        # Update inherited timers without touching explicit per-watch intervals.
        with store.conn() as db:
            from .core import iso_after
            db.execute("UPDATE watches SET next_run=? WHERE enabled=1 AND poll_minutes=0", (iso_after(CONFIG["scan_interval_minutes"]),))
    store.log("config", "Updated application settings")
    return result


@app.get("/api/query-preview")
async def query_preview(request: Request, q: str = ""):
    require(request)
    if len(q) > 250:
        raise HTTPException(422, "Query too long")
    return {"original": q, "search": search_query({"query": q, "broad_search": True}, CONFIG), "resolution": parse_resolution(q)}


@app.post("/api/watches", status_code=201)
async def add_watch(request: Request, data: WatchIn):
    require(request, True)
    if data.min_mb is not None and data.max_mb is not None and data.min_mb > data.max_mb:
        raise HTTPException(422, "Minimum size exceeds maximum")
    wid = store.save_watch(data.model_dump())
    store.log("config", f"Added watch: {data.name}")
    return {"id": wid}


@app.put("/api/watches/{wid}")
async def edit_watch(wid: int, request: Request, data: WatchIn):
    require(request, True)
    if data.min_mb is not None and data.max_mb is not None and data.min_mb > data.max_mb:
        raise HTTPException(422, "Minimum size exceeds maximum")
    try:
        store.save_watch(data.model_dump(), wid)
    except KeyError:
        raise HTTPException(404, "Watch not found")
    store.log("config", f"Updated watch: {data.name}")
    return {"id": wid}


@app.delete("/api/watches/{wid}")
async def delete_watch(wid: int, request: Request):
    require(request, True)
    try:
        store.delete_watch(wid)
    except KeyError:
        raise HTTPException(404, "Watch not found")
    store.log("config", f"Deleted watch #{wid}")
    return {"ok": True}


@app.post("/api/watches/{wid}/check")
async def check_watch(wid: int, request: Request):
    require(request, True)
    if not store.watch(wid):
        raise HTTPException(404, "Watch not found")
    if engine.lock.locked():
        raise HTTPException(409, "A watch scan is already running")
    # Run in background. Don't keep the HTTP connection open during upstream API calls.
    asyncio.create_task(engine.run_checks([wid]))
    return {"started": True}


@app.post("/api/check-all")
async def check_all(request: Request):
    require(request, True)
    if engine.lock.locked():
        raise HTTPException(409, "A scan is already running")
    ids = [w["id"] for w in store.watches() if w["enabled"]]
    asyncio.create_task(engine.run_checks(ids))
    return {"started": True, "count": len(ids)}


@app.post("/api/matches/{mid}/queue")
async def manual_queue(mid: int, request: Request):
    require(request, True)
    if not store.match(mid):
        raise HTTPException(404, "Match not found")
    if engine.lock.locked():
        raise HTTPException(409, "Scan or queue operation already running")
    async with engine.lock:
        result = await engine.queue_match(mid)
    return {"result": result}


@app.post("/api/test/{provider}")
async def test_provider(provider: str, request: Request):
    require(request, True)
    if provider not in ("hydra", "sab", "pushover"):
        raise HTTPException(404)
    try:
        return {"message": await engine.test(provider)}
    except Exception as ex:
        # Never leak tokens in an upstream HTTP error URL.
        if isinstance(ex, Exception) and hasattr(ex, "response") and getattr(ex, "response") is not None:
            raise HTTPException(502, f"Connection failed (HTTP {ex.response.status_code})")
        raise HTTPException(502, safe_error(ex)[:240])


@app.get("/api/export")
async def export(request: Request):
    require(request)
    keys = list(WatchIn.model_fields)
    entries = [{k: w[k] for k in keys} for w in store.watches()]
    payload = json.dumps({"schema_version": 1, "watches": entries}, ensure_ascii=False, indent=2)
    return Response(payload, media_type="application/json", headers={"Content-Disposition": 'attachment; filename="nzbwatch-watchlist.json"', "Cache-Control": "no-store"})


class ImportIn(BaseModel):
    watches: list[WatchIn] = Field(max_length=300)
    schema_version: int = 1


@app.post("/api/import")
async def import_watches(request: Request, data: ImportIn):
    require(request, True)
    if data.schema_version != 1:
        raise HTTPException(422, "Unsupported export schema")
    existing = {(w["query"].casefold(), w["name"].casefold()) for w in store.watches()}
    added = 0
    for w in data.watches:
        if (w.query.casefold(), w.name.casefold()) not in existing:
            store.save_watch(w.model_dump())
            existing.add((w.query.casefold(), w.name.casefold()))
            added += 1
    store.log("config", f"Imported {added} watch entries")
    return {"added": added}
