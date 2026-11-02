"""Regression tests for v0.2.6 SABnzbd status reconciliation."""
import asyncio
from urllib.parse import parse_qs
import httpx
from app.core import Engine, Store, fingerprint, now


def make_engine(tmp_path, handler):
    store = Store(str(tmp_path / "db.sqlite3"))
    wid = store.save_watch({
        "name": "Sample", "query": "Sample Show", "enabled": False, "mode": "auto",
        "match_mode": "all", "required_words": "", "excluded_words": "",
        "categories": "", "sab_category": "", "min_mb": None, "max_mb": None,
        "max_age_days": None, "max_grabs": 1, "poll_minutes": 30,
    })
    with store.conn() as db:
        db.execute("INSERT INTO matches (watch_id,title,fingerprint,link,status,nzo_id,found_at,queued_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   (wid, "Sample.Show.S01E01.1080p", fingerprint("Sample.Show.S01E01.1080p"),
                    "http://hydra/getnzb/1", "queued", "SABnzbd_nzo_123", now(), now()))
    engine = Engine(store, {
        "sab_url": "http://sab:8080", "sab_key": "sab-secret",
        "hydra_key": "hydra-secret", "pushover_key": "", "pushover_user": "",
        "notification_types": set(),
    })
    engine.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return engine, store


def run_refresh(engine):
    async def go():
        try:
            await engine.refresh_downloads()
        finally:
            await engine.client.aclose()
    asyncio.run(go())


def test_filtered_history_completes(tmp_path):
    def handler(req):
        p = parse_qs(req.url.query.decode())
        if p.get("mode") == ["history"]:
            return httpx.Response(200, json={"history": {"slots": [{
                "nzo_id": "SABnzbd_nzo_123", "status": "Completed"
            }]}})
        raise AssertionError(req.url)
    engine, store = make_engine(tmp_path, handler)
    run_refresh(engine)
    assert store.recent()["matches"][0]["status"] == "completed"
    assert sum(e["type"] == "completed" for e in store.recent()["events"]) == 1


def test_paginated_unfiltered_history_recovery(tmp_path):
    calls = []
    def handler(req):
        p = parse_qs(req.url.query.decode())
        calls.append(p)
        mode = p["mode"][0]
        if mode == "queue":
            return httpx.Response(200, json={"queue": {"slots": []}})
        if mode == "history":
            if "nzo_ids" in p:
                return httpx.Response(200, json={"history": {"slots": []}})
            if p.get("archive", ["0"])[0] == "1":
                return httpx.Response(200, json={"history": {"slots": []}})
            start = int(p.get("start", ["0"])[0])
            slots = [{"nzo_id": f"other_{i}", "status": "Completed"}
                     for i in range(start, start + 200)]
            if start == 200:
                slots.append({"nzo_id": "SABnzbd_nzo_123", "status": "Completed"})
            return httpx.Response(200, json={"history": {"slots": slots}})
        raise AssertionError(req.url)
    engine, store = make_engine(tmp_path, handler)
    run_refresh(engine)
    assert store.recent()["matches"][0]["status"] == "completed"
    assert any(x.get("start") == ["200"] for x in calls)


def test_archive_completion_recovered(tmp_path):
    def handler(req):
        p = parse_qs(req.url.query.decode())
        if p["mode"] == ["queue"]:
            return httpx.Response(200, json={"queue": {"slots": []}})
        if p["mode"] == ["history"]:
            if p.get("archive") == ["1"]:
                return httpx.Response(200, json={"history": {"slots": [{
                    "nzo_id": "SABnzbd_nzo_123", "status": "Completed"}]}})
            return httpx.Response(200, json={"history": {"slots": []}})
        raise AssertionError(req.url)
    engine, store = make_engine(tmp_path, handler)
    run_refresh(engine)
    assert store.recent()["matches"][0]["status"] == "completed"


def test_still_in_queue_does_not_complete(tmp_path):
    def handler(req):
        p = parse_qs(req.url.query.decode())
        if p["mode"] == ["history"]:
            return httpx.Response(200, json={"history": {"slots": []}})
        if p["mode"] == ["queue"]:
            return httpx.Response(200, json={"queue": {"slots": [{
                "nzo_id": "SABnzbd_nzo_123", "status": "Downloading"}]}})
        raise AssertionError(req.url)
    engine, store = make_engine(tmp_path, handler)
    run_refresh(engine)
    assert store.recent()["matches"][0]["status"] == "queued"
    assert store.recent()["matches"][0]["note"] is None


def test_missing_job_is_annotated_not_completed(tmp_path):
    def handler(req):
        p = parse_qs(req.url.query.decode())
        return httpx.Response(200, json={p["mode"][0]: {"slots": []}})
    engine, store = make_engine(tmp_path, handler)
    run_refresh(engine)
    match = store.recent()["matches"][0]
    assert match["status"] == "queued"
    assert "unverified" in match["note"]


def test_failure_is_terminal(tmp_path):
    def handler(req):
        return httpx.Response(200, json={"history": {"slots": [{
            "nzo_id": "SABnzbd_nzo_123", "status": "Failed", "fail_message": "CRC failed"
        }]}})
    engine, store = make_engine(tmp_path, handler)
    run_refresh(engine)
    match = store.recent()["matches"][0]
    assert match["status"] == "failed_download"
    assert match["note"] == "CRC failed"


def test_sab_api_failure_does_not_override_status(tmp_path):
    def handler(req):
        return httpx.Response(502, text="temporarily unavailable")
    engine, store = make_engine(tmp_path, handler)
    run_refresh(engine)
    assert store.recent()["matches"][0]["status"] == "queued"
    assert store.recent()["matches"][0]["note"] is None
