"""Watchlist storage, NZBHydra2/SABnzbd integration and scheduled job runner."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

import httpx

log = logging.getLogger("nzbwatch")
UTC = timezone.utc
NEWZNAB = "http://www.newznab.com/DTD/2010/feeds/attributes/"


def safe_error(ex: Exception) -> str:
    """Avoid exposing credential-bearing request URLs in logs and the UI."""
    if isinstance(ex, httpx.HTTPStatusError):
        return f"Upstream HTTP {ex.response.status_code}"
    if isinstance(ex, httpx.RequestError):
        return f"Upstream network failure ({type(ex).__name__})"
    return str(ex)[:500]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def iso_after(minutes: int) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def normalize_title(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", s.lower()))


def fingerprint(s: str) -> str:
    return hashlib.sha256(normalize_title(s).encode()).hexdigest()


def words(s: str) -> list[str]:
    return [w.strip() for w in re.split(r"[,\n]+", s) if w.strip()]


def matching(watch: dict, result: dict) -> bool:
    title = normalize_title(result["title"])
    query = normalize_title(watch["query"])
    tokens = query.split()
    mode = watch.get("match_mode", "all")
    if not tokens or not title:
        return False
    if mode == "phrase" and query not in title:
        return False
    if mode == "all" and not all(t in title.split() for t in tokens):
        return False
    if mode == "any" and not any(t in title.split() for t in tokens):
        return False
    if any(normalize_title(x) not in title for x in words(watch.get("required_words", ""))):
        return False
    if any(normalize_title(x) in title for x in words(watch.get("excluded_words", ""))):
        return False
    size = result.get("size_bytes") or 0
    min_mb = watch.get("min_mb")
    max_mb = watch.get("max_mb")
    if min_mb is not None and (not size or size < min_mb * 1024 * 1024):
        return False
    if max_mb is not None and (not size or size > max_mb * 1024 * 1024):
        return False
    max_age = watch.get("max_age_days")
    pub = result.get("published")
    if max_age is not None and pub:
        try:
            if datetime.fromisoformat(pub) < datetime.now(UTC) - timedelta(days=max_age):
                return False
        except ValueError:
            pass
    return True


def parse_rss(data: bytes) -> list[dict]:
    """The Newznab XML variant is stable across Hydra versions."""
    if len(data) > 4_000_000:
        raise ValueError("Search response exceeds 4 MB")
    root = ET.fromstring(data)
    errors = root.find(".//error")
    if errors is not None:
        raise ValueError(f"Hydra search error: {errors.get('description', 'unknown')}")
    items = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        enc = item.find("enclosure")
        link = (item.findtext("link") or (enc.get("url") if enc is not None else "") or "").strip()
        if not link:
            continue
        attrs = {a.get("name"): a.get("value") for a in item.findall(f"{{{NEWZNAB}}}attr")}
        raw_size = attrs.get("size") or (enc.get("length") if enc is not None else "") or "0"
        try:
            size = int(raw_size)
        except (TypeError, ValueError):
            size = 0
        published = None
        if item.findtext("pubDate"):
            try:
                published = parsedate_to_datetime(item.findtext("pubDate")).astimezone(UTC).isoformat(timespec="seconds")
            except (TypeError, ValueError):
                pass
        items.append({"title": title, "link": link, "guid": item.findtext("guid") or "", "size_bytes": size, "published": published, "category": attrs.get("category", "")})
    return items


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @contextmanager
    def conn(self):
        db = sqlite3.connect(self.path, timeout=20)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=20000")
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def init_db(self):
        with self.conn() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS watches (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, query TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, mode TEXT NOT NULL DEFAULT 'auto',
                match_mode TEXT NOT NULL DEFAULT 'all', required_words TEXT NOT NULL DEFAULT '',
                excluded_words TEXT NOT NULL DEFAULT '', categories TEXT NOT NULL DEFAULT '',
                sab_category TEXT NOT NULL DEFAULT '', min_mb REAL, max_mb REAL,
                max_age_days INTEGER, max_grabs INTEGER NOT NULL DEFAULT 1,
                grab_count INTEGER NOT NULL DEFAULT 0, poll_minutes INTEGER NOT NULL DEFAULT 30,
                last_checked TEXT, next_run TEXT, last_error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY, watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
                title TEXT NOT NULL, fingerprint TEXT NOT NULL, size_bytes INTEGER,
                published TEXT, link TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'found',
                nzo_id TEXT, note TEXT, found_at TEXT NOT NULL, queued_at TEXT, finished_at TEXT,
                UNIQUE(watch_id, fingerprint)
            );
            CREATE INDEX IF NOT EXISTS matches_nzo ON matches(nzo_id);
            CREATE TABLE IF NOT EXISTS grabs (
                fingerprint TEXT PRIMARY KEY, match_id INTEGER NOT NULL,
                nzo_id TEXT NOT NULL, queued_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, type TEXT NOT NULL, message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """)

    @staticmethod
    def rows(cursor):
        return [dict(r) for r in cursor.fetchall()]

    def watches(self):
        with self.conn() as db:
            return self.rows(db.execute("SELECT * FROM watches ORDER BY enabled DESC, name COLLATE NOCASE"))

    def watch(self, wid: int):
        with self.conn() as db:
            row = db.execute("SELECT * FROM watches WHERE id=?", (wid,)).fetchone()
            return dict(row) if row else None

    def save_watch(self, data: dict, wid: int | None = None):
        columns = ["name", "query", "enabled", "mode", "match_mode", "required_words", "excluded_words", "categories", "sab_category", "min_mb", "max_mb", "max_age_days", "max_grabs", "poll_minutes"]
        vals = [data[c] for c in columns]
        with self.conn() as db:
            if wid is None:
                c = db.execute(f"INSERT INTO watches ({','.join(columns)},created_at,updated_at,next_run) VALUES ({','.join(['?']*len(columns))},?,?,?)", (*vals, now(), now(), now()))
                return c.lastrowid
            cur = db.execute(f"UPDATE watches SET {','.join(c+'=?' for c in columns)},updated_at=?,next_run=? WHERE id=?", (*vals, now(), now(), wid))
            if not cur.rowcount:
                raise KeyError(wid)
            return wid

    def delete_watch(self, wid: int):
        with self.conn() as db:
            if not db.execute("DELETE FROM watches WHERE id=?", (wid,)).rowcount:
                raise KeyError(wid)

    def log(self, type_: str, message: str):
        with self.conn() as db:
            db.execute("INSERT INTO events(type,message,created_at) VALUES(?,?,?)", (type_, message[:1200], now()))
            db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 1000)")

    def recent(self):
        with self.conn() as db:
            matches = self.rows(db.execute("SELECT m.id,m.watch_id,w.name AS watch_name,m.title,m.size_bytes,m.published,m.status,m.nzo_id,m.note,m.found_at,m.queued_at,m.finished_at FROM matches m JOIN watches w ON w.id=m.watch_id ORDER BY m.id DESC LIMIT 250"))
            events = self.rows(db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 80"))
            counts = dict(db.execute("SELECT (SELECT count(*) FROM watches) watches, (SELECT count(*) FROM watches WHERE enabled=1) active, (SELECT count(*) FROM matches WHERE status='found') found, (SELECT count(*) FROM matches WHERE status='queued') queued, (SELECT count(*) FROM matches WHERE status='completed') completed").fetchone())
        return {"matches": matches, "events": events, "counts": counts}

    def match(self, mid: int):
        with self.conn() as db:
            x = db.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
            return dict(x) if x else None


class Engine:
    def __init__(self, store: Store, config: dict):
        self.store, self.config = store, config
        self.lock = asyncio.Lock()
        self.poll_task = None
        self.stop = asyncio.Event()
        self.client = None

    async def startup(self):
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=8.0), follow_redirects=False)
        self.poll_task = asyncio.create_task(self.loop(), name="nzbwatch-scheduler")

    async def shutdown(self):
        self.stop.set()
        if self.poll_task:
            await self.poll_task
        if self.client:
            await self.client.aclose()

    async def loop(self):
        while not self.stop.is_set():
            try:
                await self.check_due()
                await self.refresh_downloads()
            except Exception as ex:
                log.error("Scheduler failure: %s", safe_error(ex))
                self.store.log("error", f"Scheduler: {safe_error(ex)}")
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    async def hydra_search(self, watch: dict):
        params = {"t": "search", "q": watch["query"], "apikey": self.config["hydra_key"], "limit": 100}
        if watch.get("categories"):
            params["cat"] = watch["categories"]
        r = await self.client.get(self.config["hydra_url"] + "/api", params=params)
        r.raise_for_status()
        return parse_rss(r.content)

    async def push(self, title: str, message: str, kind: str):
        if not self.config["pushover_key"] or not self.config["pushover_user"]:
            return
        if kind not in self.config["notification_types"]:
            return
        try:
            r = await self.client.post("https://api.pushover.net/1/messages.json", data={"token": self.config["pushover_key"], "user": self.config["pushover_user"], "title": title[:250], "message": message[:1024]})
            r.raise_for_status()
            if r.json().get("status") != 1:
                raise ValueError("Pushover rejected notification")
        except Exception as ex:
            log.warning("Pushover failed: %s", ex)
            self.store.log("warning", f"Pushover notification failed: {ex}")

    async def check_due(self):
        if self.lock.locked():
            return
        with self.store.conn() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM watches WHERE enabled=1 AND (next_run IS NULL OR next_run<=?) ORDER BY COALESCE(next_run,'') LIMIT 100", (now(),))]
        if ids:
            await self.run_checks(ids)

    async def run_checks(self, ids: list[int]):
        async with self.lock:
            for wid in ids:
                watch = self.store.watch(wid)
                if not watch or not watch["enabled"]:
                    continue
                with self.store.conn() as db:
                    db.execute("UPDATE watches SET next_run=?,last_checked=?,last_error=NULL WHERE id=?", (iso_after(watch["poll_minutes"]), now(), wid))
                try:
                    result = await self.check_watch(watch)
                    self.store.log("search", f"{watch['name']}: {result['searched']} results, {result['matched']} matched, {result['new']} new")
                except Exception as ex:
                    log.error("Watch '%s' failed: %s", watch["name"], safe_error(ex))
                    msg = safe_error(ex)
                    with self.store.conn() as db:
                        db.execute("UPDATE watches SET last_error=? WHERE id=?", (msg, wid))
                    self.store.log("error", f"{watch['name']}: {msg}")

    async def check_watch(self, watch: dict):
        if not self.config["hydra_url"] or not self.config["hydra_key"]:
            raise RuntimeError("NZBHydra2 settings are missing")
        results = await self.hydra_search(watch)
        eligible = [r for r in results if matching(watch, r)]
        eligible.sort(key=lambda r: r.get("published") or "", reverse=True)
        new_count = 0
        pushed = 0
        queued_this_scan = 0
        for r in eligible:
            fp = fingerprint(r["title"])
            with self.store.conn() as db:
                existing = db.execute("SELECT id FROM matches WHERE watch_id=? AND fingerprint=?", (watch["id"], fp)).fetchone()
                if existing:
                    mid = existing[0]
                else:
                    c = db.execute("INSERT INTO matches(watch_id,title,fingerprint,size_bytes,published,link,status,found_at) VALUES(?,?,?,?,?,?,?,?)", (watch["id"], r["title"], fp, r["size_bytes"], r["published"], r["link"], "found", now()))
                    mid = c.lastrowid
                    new_count += 1
            if not existing:
                self.store.log("found", f"{watch['name']}: {r['title']}")
                # Avoid sending dozens of pushes when an existing broad search is added.
                if pushed < 5:
                    await self.push("NZB found", f"{watch['name']}: {r['title']}", "found")
                    pushed += 1
            if watch["mode"] == "auto" and queued_this_scan < self.config.get("max_queues_per_scan", 3):
                outcome = await self.queue_match(mid)
                if outcome == "queued":
                    queued_this_scan += 1
                    if watch["max_grabs"] and not self.store.watch(watch["id"])["enabled"]:
                        break  # One-off watches automatically stop once their quota is reached.
        if new_count > pushed and self.config["pushover_key"]:
            await self.push("NZBWatch search summary", f"{watch['name']}: {new_count} new matches found. Showing first {pushed} individually.", "found")
        return {"searched": len(results), "matched": len(eligible), "new": new_count}

    async def queue_match(self, mid: int):
        m = self.store.match(mid)
        if not m:
            raise KeyError(mid)
        if m["status"] not in ("found", "failed"):
            return "already_handled"
        w = self.store.watch(m["watch_id"])
        if not w:
            raise KeyError(m["watch_id"])
        with self.store.conn() as db:
            used = db.execute("SELECT grab_count FROM watches WHERE id=?", (w["id"],)).fetchone()[0]
            globally = db.execute("SELECT nzo_id FROM grabs WHERE fingerprint=?", (m["fingerprint"],)).fetchone()
        if globally:
            with self.store.conn() as db:
                db.execute("UPDATE matches SET status='skipped',note=? WHERE id=?", ("Already queued for another watch (SAB ID: " + globally[0] + ")", mid))
            return "duplicate"
        if w["max_grabs"] != 0 and used >= w["max_grabs"]:
            return "limit"
        try:
            if not self.config["sab_url"] or not self.config["sab_key"]:
                raise RuntimeError("SABnzbd settings are missing")
            # Hydra-generated URLs normally point to Hydra. Never fetch external indexer hosts.
            url = urljoin(self.config["hydra_url"] + "/", m["link"])
            source = urlsplit(url)
            expected = urlsplit(self.config["hydra_url"])
            if source.scheme not in ("http", "https") or (source.hostname, source.port) != (expected.hostname, expected.port):
                raise ValueError("NZB link is outside configured NZBHydra2 host")
            # Hydra may proxy NZBs directly OR reply with a redirect to its indexer.
            # If it redirects, let SABnzbd request the authenticated Hydra URL itself.
            # Both Hydra and SAB must then be network-reachable from SAB's container.
            params = {} if "apikey=" in source.query.lower() else {"apikey": self.config["hydra_key"]}
            async with self.client.stream("GET", url, params=params) as r:
                if r.is_redirect:
                    content = None
                else:
                    r.raise_for_status()
                    if int(r.headers.get("content-length", "0")) > 20_000_000:
                        raise ValueError("NZB exceeds 20 MB")
                    chunks, count = [], 0
                    async for chunk in r.aiter_bytes():
                        count += len(chunk)
                        if count > 20_000_000:
                            raise ValueError("NZB exceeds 20 MB")
                        chunks.append(chunk)
                    content = b"".join(chunks)
            sab_params = {"apikey": self.config["sab_key"], "output": "json", "nzbname": m["title"]}
            if w["sab_category"]:
                sab_params["cat"] = w["sab_category"]
            if content is None:
                # QueryParams encodes URL and credentials for forwarding to SAB. Do
                # not use the destination of Hydra's redirect, only the original link.
                sab_params["mode"] = "addurl"
                from urllib.parse import parse_qsl, urlencode, urlunsplit
                q = dict(parse_qsl(source.query, keep_blank_values=True))
                q.setdefault("apikey", self.config["hydra_key"])
                download_url = urlunsplit((source.scheme, source.netloc, source.path, urlencode(q), source.fragment))
                sab_params["name"] = download_url
                res = await self.client.post(self.config["sab_url"] + "/api", params=sab_params)
            else:
                if b"<nzb" not in content[:4000].lower():
                    raise ValueError("Hydra did not return an NZB document")
                sab_params["mode"] = "addfile"
                res = await self.client.post(self.config["sab_url"] + "/api", params=sab_params, files={"nzbfile": ("watch.nzb", content, "application/x-nzb")})
            res.raise_for_status()
            data = res.json()
            ids = data.get("nzo_ids") or []
            if not data.get("status") or not ids:
                raise ValueError(f"SABnzbd rejected file: {data.get('error', 'unknown error')}")
            nzo_id = str(ids[0])
            with self.store.conn() as db:
                # Keep a global unique title fingerprint across watch rules.
                db.execute("INSERT INTO grabs(fingerprint,match_id,nzo_id,queued_at) VALUES(?,?,?,?)", (m["fingerprint"], mid, nzo_id, now()))
                db.execute("UPDATE matches SET status='queued',nzo_id=?,queued_at=?,note=NULL WHERE id=?", (nzo_id, now(), mid))
                db.execute("UPDATE watches SET grab_count=grab_count+1, enabled=CASE WHEN max_grabs > 0 AND grab_count+1 >= max_grabs THEN 0 ELSE enabled END WHERE id=?", (w["id"],))
            self.store.log("queued", f"{w['name']}: {m['title']}")
            await self.push("NZB queued", f"{m['title']} queued in SABnzbd", "queued")
            return "queued"
        except Exception as ex:
            with self.store.conn() as db:
                db.execute("UPDATE matches SET status='failed',note=? WHERE id=?", (safe_error(ex), mid))
            self.store.log("error", f"Queue failed for {m['title']}: {safe_error(ex)}")
            return "failed"

    async def refresh_downloads(self):
        if not self.config["sab_url"] or not self.config["sab_key"]:
            return
        with self.store.conn() as db:
            rows = self.store.rows(db.execute("SELECT id,nzo_id,title FROM matches WHERE status='queued' AND nzo_id IS NOT NULL ORDER BY id DESC LIMIT 1000"))
        if not rows:
            return
        try:
            # SAB supports filtering by nzo_ids. Query in batches to avoid huge URLs.
            for i in range(0, len(rows), 50):
                subset = rows[i:i + 50]
                ids = ",".join(x["nzo_id"] for x in subset)
                data = await self.sab_get("history", {"nzo_ids": ids, "limit": 100})
                hist = {str(x.get("nzo_id")): x for x in data.get("history", {}).get("slots", [])}
                for row in subset:
                    job = hist.get(row["nzo_id"])
                    if not job:
                        continue
                    raw_status = str(job.get("status", ""))
                    if raw_status not in ("Completed", "Failed"):
                        continue
                    status = "completed" if raw_status == "Completed" else "failed_download"
                    with self.store.conn() as db:
                        db.execute("UPDATE matches SET status=?,finished_at=?,note=? WHERE id=? AND status='queued'", (status, now(), str(job.get("fail_message", ""))[:500] or None, row["id"]))
                    self.store.log(status, f"{row['title']}: {raw_status}")
                    await self.push("NZB download " + raw_status.lower(), row["title"], "completed" if status == "completed" else "failed")
        except Exception as ex:
            log.warning("Could not refresh SABnzbd history: %s", safe_error(ex))
            self.store.log("warning", f"SAB history refresh failed: {safe_error(ex)}")

    async def sab_get(self, mode: str, extra: dict | None = None):
        if not self.config["sab_url"] or not self.config["sab_key"]:
            raise RuntimeError("SABnzbd settings are missing")
        r = await self.client.get(self.config["sab_url"] + "/api", params={"mode": mode, "apikey": self.config["sab_key"], "output": "json", **(extra or {})})
        r.raise_for_status()
        value = r.json()
        if isinstance(value, dict) and value.get("error"):
            raise ValueError(str(value["error"]))
        return value

    async def test(self, provider: str):
        if provider == "hydra":
            if not self.config["hydra_url"] or not self.config["hydra_key"]:
                raise ValueError("Hydra URL/API key not configured")
            r = await self.client.get(self.config["hydra_url"] + "/api", params={"t": "caps", "apikey": self.config["hydra_key"]})
            r.raise_for_status()
            ET.fromstring(r.content)
            return "Connected to NZBHydra2"
        if provider == "sab":
            d = await self.sab_get("version")
            return f"Connected to SABnzbd ({d.get('version', 'version unknown')})"
        if provider == "pushover":
            if not self.config["pushover_key"] or not self.config["pushover_user"]:
                raise ValueError("Pushover token/user not configured")
            r = await self.client.post("https://api.pushover.net/1/messages.json", data={"token": self.config["pushover_key"], "user": self.config["pushover_user"], "title": "NZBWatch test", "message": "Your notifications are connected."})
            r.raise_for_status()
            if r.json().get("status") != 1:
                raise ValueError("Pushover rejected test")
            return "Test notification sent"
        raise ValueError("Unknown provider")
