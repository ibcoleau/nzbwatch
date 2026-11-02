#!/usr/bin/env python3
"""Apply NZBWatch 0.2.6 SAB completion reconciliation fix to v0.2.5 checkout.
This patch alters only app/core.py, VERSION and adds a regression test file.
"""
from pathlib import Path
import sys

FIX = '''    async def refresh_downloads(self):
        """Reconcile queued NZBWatch jobs against SAB's queue and history.

        SAB may omit matching jobs from a filtered history response (for example,
        due to history view/limit behaviour). Querying only that response left
        completed downloads looking permanently queued. Never infer completion
        from absence: only SAB's explicit Completed / Failed status is terminal.
        """
        if not self.config["sab_url"] or not self.config["sab_key"]:
            return
        with self.store.conn() as db:
            rows = self.store.rows(db.execute(
                "SELECT id,nzo_id,title,note FROM matches "
                "WHERE status='queued' AND nzo_id IS NOT NULL "
                "ORDER BY id DESC LIMIT 1000"
            ))
        if not rows:
            return

        def get_slots(response: dict, section: str) -> list[dict]:
            value = response.get(section, {})
            slots = value.get("slots", []) if isinstance(value, dict) else []
            return [slot for slot in slots if isinstance(slot, dict)] if isinstance(slots, list) else []

        def index_slots(slots: list[dict]) -> dict[str, dict]:
            return {str(slot["nzo_id"]): slot for slot in slots if slot.get("nzo_id")}

        # Limit pagination in case SAB ignores nzo_ids or history contains very
        # large numbers of unrelated entries. A missing item remains queued and
        # is clearly annotated rather than incorrectly marked as completed.
        async def find_history_pages(target: set[str], *, archive: bool = False) -> dict[str, dict]:
            found = {}
            seen = set()
            for start in range(0, 1000, 200):
                response = await self.sab_get("history", {
                    "start": start, "limit": 200, "archive": int(archive)
                })
                slots = get_slots(response, "history")
                page = index_slots(slots)
                found.update({key: job for key, job in page.items() if key in target})
                # Stop if pagination isn't supported / same items are returned.
                fresh = set(page) - seen
                if target <= set(found) or not slots or not fresh or len(slots) < 200:
                    break
                seen.update(page)
            return found

        for i in range(0, len(rows), 50):
            subset = rows[i:i + 50]
            ids = {str(row["nzo_id"]) for row in subset}
            try:
                # First use the supported ID filter: usually a single cheap call.
                data = await self.sab_get("history", {
                    "nzo_ids": ",".join(sorted(ids)), "start": 0, "limit": 200
                })
                history = index_slots(get_slots(data, "history"))
                missing = ids - set(history)
                queue = {}
                if missing:
                    q = await self.sab_get("queue", {
                        "nzo_ids": ",".join(sorted(missing)), "start": 0,
                        "limit": 200
                    })
                    queue = index_slots(get_slots(q, "queue"))
                    # Some SAB versions don't honour filtering as expected.
                    # First attempt an unfiltered, bounded paginated lookup.
                    not_found = missing - set(queue)
                    if not_found:
                        history.update(await find_history_pages(not_found))
                        not_found -= set(history)
                    # Archived history can contain a completed item no longer
                    # present in the default history view.
                    if not_found:
                        history.update(await find_history_pages(not_found, archive=True))

                for row in subset:
                    nzo_id = str(row["nzo_id"])
                    job = history.get(nzo_id)
                    raw_status = str(job.get("status", "")).strip().casefold() if job else ""
                    if raw_status in ("completed", "failed"):
                        status = "completed" if raw_status == "completed" else "failed_download"
                        failure = str(job.get("fail_message") or "") if raw_status == "failed" else ""
                        note = safe_upstream_description(failure, secrets=(
                            self.config["sab_key"], self.config.get("hydra_key", "")
                        )) if failure else None
                        with self.store.conn() as db:
                            updated = db.execute(
                                "UPDATE matches SET status=?,finished_at=?,note=? "
                                "WHERE id=? AND status='queued'",
                                (status, now(), note, row["id"])
                            ).rowcount
                        # Status transition is idempotent: one event + push max.
                        if updated:
                            self.store.log(status, f"{row['title']}: {'Completed' if status == 'completed' else 'Failed'}")
                            await self.push(
                                "NZB download completed" if status == "completed" else "NZB download failed",
                                row["title"], "completed" if status == "completed" else "failed"
                            )
                        continue

                    # A job might be downloading, processing, or paused; keep
                    # queued until SAB confirms a terminal status.
                    if job or nzo_id in queue:
                        if row["note"] and row["note"].startswith("SAB status unverified:"):
                            with self.store.conn() as db:
                                db.execute("UPDATE matches SET note=NULL WHERE id=? AND status='queued'", (row["id"],))
                        continue

                    message = (
                        "SAB status unverified: job not found in current queue or "
                        "recent/archived history. Check SAB history retention or job ID."
                    )
                    if row["note"] != message:
                        with self.store.conn() as db:
                            db.execute("UPDATE matches SET note=? WHERE id=? AND status='queued'", (message, row["id"]))
                        self.store.log("warning", f"{row['title']}: {message}")
            except Exception as ex:
                # Keep queued states intact if SAB is temporarily unavailable.
                log.warning("Could not refresh SABnzbd status: %s", safe_error(ex))
                self.store.log("warning", f"SAB status refresh failed: {safe_error(ex)}")

'''

TEST = r'''"""Regression tests for v0.2.6 SABnzbd status reconciliation."""
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
'''

def main():
    root = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else Path.cwd()
    target = root / "app" / "core.py"
    version = root / "VERSION"
    if not target.is_file() or not version.is_file():
        raise SystemExit(f"Not an NZBWatch checkout (app/core.py and VERSION required): {root}")
    current = version.read_text(encoding="utf-8").strip()
    if current not in ("0.2.5", "0.2.6"):
        raise SystemExit(f"Expected NZBWatch v0.2.5, got {current!r}; not making changes")
    source = target.read_text(encoding="utf-8")
    marker = "    async def refresh_downloads(self):"
    end = "    async def sab_get(self, mode: str, extra: dict | None = None):"
    if marker not in source or end not in source:
        raise SystemExit("Expected SAB refresh method not found; no changes made")
    if source.count(marker) != 1 or source.count(end) != 1:
        raise SystemExit("Ambiguous source methods; no changes made")
    original = source.split(marker,1)[1].split(end,1)[0]
    if "find_history_pages" in original:
        print("v0.2.6 fix already applied")
        return
    if not all(token in original for token in ("nzo_ids", "history", "status='queued'", "finished_at")):
        raise SystemExit("Unexpected SAB reconciliation code; refusing to overwrite local changes")
    patched = source[:source.index(marker)] + FIX + source[source.index(end):]
    target.write_text(patched, encoding="utf-8")
    (root/"tests").mkdir(exist_ok=True)
    (root/"tests"/"test_sab_reconciliation_026.py").write_text(TEST, encoding="utf-8")
    version.write_text("0.2.6\n", encoding="utf-8")
    print("Applied NZBWatch v0.2.6 SAB reconciliation patch")
    print("Modified app/core.py and VERSION; added tests/test_sab_reconciliation_026.py")
    print("Next: python -m pytest -q")

if __name__ == "__main__":
    main()
