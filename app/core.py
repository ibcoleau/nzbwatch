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
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
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


# Only remove the technical suffix of a release name. Keep the title, year,
# season/episode and other identifying text to avoid broad false matches.
TECHNICAL = re.compile(
    r"^(?:2160p|1080p|720p|576p|480p|360p|4k|uhd|fhd|hd|sd|"
    r"web[.-]?dl|webrip|bluray|blu[.-]?ray|bdrip|brrip|hdtv|hddvd|"
    r"dvdrip|hdrip|hdr|dv|dolby[.-]?vision|av1|x26[45]|h[. ]?26[45]|"
    r"hevc|avc|mp4|mkv|avi|aac|dts|ddp?[257]?|atmos|proper|repack|"
    r"internal|remux|xxx)$", re.I
)
RES_PATTERN = re.compile(r"(?<![a-z0-9])(?:2160p|1080p|720p|576p|480p|360p|4k|uhd)(?![a-z0-9])", re.I)
PIXEL_PATTERN = re.compile(r"(?<!\d)(?:3840\s*[x×]\s*2160|1920\s*[x×]\s*1080|1280\s*[x×]\s*720)(?!\d)", re.I)
RES_HEIGHT = {"2160p": 2160, "1080p": 1080, "720p": 720, "576p": 576, "480p": 480, "360p": 360}


def parse_resolution(title: str) -> str | None:
    """Recognise explicit height and common 4K/UHD and pixel-dimension markers."""
    m = RES_PATTERN.search(title)
    if m:
        label = m.group().lower()
        return "2160p" if label in ("4k", "uhd") else label
    m = PIXEL_PATTERN.search(title)
    if m:
        dimension = m.group().lower().replace(" ", "")
        return "2160p" if "2160" in dimension else "1080p" if "1080" in dimension else "720p"
    return None


def simplify_query(query: str, strip_groups: bool = True) -> str:
    """Strip technical release suffixes; avoid treating a full filename as a search title."""
    value = query.strip()
    value = re.sub(r"(?i)\.(?:mkv|mp4|avi|nzb)$", "", value)
    # The -GROUP token is only stripped when a release-tech marker appears.
    if strip_groups and re.search(r"(?i)(?:\.|\s|_)(?:2160p|1080p|720p|480p|web[.-]?dl|webrip|bluray|mp4|mkv|xxx)(?:\.|\s|_|-)", value):
        value = re.sub(r"-([a-z0-9]{2,24})$", "", value, flags=re.I)
    value = re.sub(r"[._]+", " ", value)
    parts = value.split()
    kept = []
    for part in parts:
        if TECHNICAL.fullmatch(part):
            break
        kept.append(part)
    cleaned = " ".join(kept).strip(" -")
    # Do not generate an empty/one-letter query from overzealous cleaning.
    return cleaned if len(normalize_title(cleaned).replace(" ", "")) >= 3 else query.strip()


def search_query(watch: dict, config: dict) -> str:
    if config.get("clean_search_terms", True) and watch.get("broad_search", 1):
        return simplify_query(watch["query"], config.get("strip_release_groups", True))
    return watch["query"].strip()


def quality_allowed(watch: dict, result: dict, config: dict) -> bool:
    mode = watch.get("resolution_mode", "inherit")
    enabled = config.get("resolution_enabled", False) if mode == "inherit" else mode == "on"
    if not enabled:
        return True
    resolution = result.get("resolution") or parse_resolution(result["title"])
    if not resolution:
        return config.get("allow_unknown_resolution", False)
    return RES_HEIGHT[resolution] >= RES_HEIGHT[config.get("fallback_min_resolution", "720p")]


def quality_sort_key(result: dict, config: dict) -> tuple:
    """Sort by preferred resolution, then descending fallback quality and date."""
    order = config.get("resolution_order", ["1080p", "2160p", "720p", "480p"])
    resolution = result.get("resolution") or parse_resolution(result["title"])
    if resolution in order:
        rank = order.index(resolution)
    elif resolution:
        rank = len(order) + (2160 - RES_HEIGHT[resolution]) / 10000
    else:
        rank = len(order) + 100
    return (rank, -(datetime.fromisoformat(result["published"]).timestamp() if result.get("published") else 0))


def fingerprint(s: str) -> str:
    return hashlib.sha256(normalize_title(s).encode()).hexdigest()


def content_fingerprint(s: str) -> str:
    # For quality-tagged releases, dedupe multiple resolutions of the same title.
    # Unclassified material retains the original exact-title fingerprint.
    if parse_resolution(s):
        return fingerprint(simplify_query(s))
    return fingerprint(s)


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
        # <link> may be an indexer details page.  The enclosure URL is the
        # Newznab download URL and should be preferred when present.
        link = ((enc.get("url") if enc is not None else "") or item.findtext("link") or "").strip()
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



def authenticated_hydra_url(base_url: str, result_link: str, api_key: str) -> str:
    """Return a same-origin NZB URL with the *current* Hydra API key.

    Hydra can return RSS enclosure URLs with empty or old `apikey` query
    parameters.  Looking only for the presence of the parameter mistakenly
    used those values instead of the current configured key.
    """
    url = urljoin(base_url.rstrip("/") + "/", result_link)
    source, expected = urlsplit(url), urlsplit(base_url)
    if (source.scheme not in ("http", "https") or
        source.hostname != expected.hostname or source.port != expected.port or
        source.username is not None or source.password is not None):
        raise ValueError("NZB link is outside configured NZBHydra2 host")
    # Reject lookalike domains and avoid sending the Hydra key to foreign hosts.
    query = [(k, v) for k, v in parse_qsl(source.query, keep_blank_values=True)
             if k.lower() != "apikey"]
    query.append(("apikey", api_key))
    return urlunsplit((source.scheme, source.netloc, source.path, urlencode(query), ""))


def safe_upstream_description(message: str, secrets: tuple[str, ...] = ()) -> str:
    """Display a useful API error while masking credentials and request URLs."""
    text = " ".join(str(message).split())[:800]
    # Mask key-value pairs and URLs: upstream errors can echo their request.
    text = re.sub(r"(?i)\b(apikey|api_key|token|password|api-key|access_token)\b\s*[:=]\s*[^\s&,;<>\"']+",
                  r"\1=[redacted]", text)
    text = re.sub(r"https?://[^\s<>\"']+", "[URL redacted]", text, flags=re.I)
    for value in secrets:
        if value:
            text = text.replace(value, "[redacted]")
    return text[:280] or "No reason provided"


def nzb_upload_info(content: bytes, content_type: str = "", *, secrets: tuple[str, ...] = ()) -> tuple[str, str]:
    """Validate a Hydra download response without mistaking an HTML page for NZB.

    SABnzbd accepts regular NZBs and supported compressed NZB archives. The
    filename extension is significant for its file-upload parser.
    """
    if not content:
        raise ValueError("Hydra returned an empty download response")
    if content.startswith(b"\x1f\x8b"):
        return ("watch.nzb.gz", "application/gzip")
    if content.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return ("watch.nzb.zip", "application/zip")
    if content.startswith(b"Rar!\x1a\x07"):
        return ("watch.nzb.rar", "application/vnd.rar")
    # XML declaration, comments and namespaces may precede the <nzb> tag.
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        root = None
    if root is not None:
        name = root.tag.rsplit("}", 1)[-1].lower() if isinstance(root.tag, str) else ""
        if name == "nzb":
            return ("watch.nzb", "application/x-nzb")
        if name == "error":
            # Newznab errors are HTTP 200 XML, e.g. <error code="100"
            # description="Incorrect API key"/>. Preserve the actual reason.
            code = safe_upstream_description(root.get("code", ""), secrets)
            description = safe_upstream_description(root.get("description") or root.text or "No reason provided", secrets)
            prefix = f"Hydra NZB download error (code {code})" if root.get("code") else "Hydra NZB download error"
            raise ValueError(f"{prefix}: {description}")
        if name in ("html", "rss"):
            raise ValueError(f"Hydra returned {name.upper()} instead of an NZB (check the download link and Hydra API access)")
    mime = content_type.split(";", 1)[0].strip().lower() or "unknown"
    if mime in ("text/html", "application/xhtml+xml") or content.lstrip()[:100].lower().startswith((b"<!doctype html", b"<html")):
        raise ValueError("Hydra returned an HTML page instead of an NZB (check the download link and Hydra API access)")
    if mime == "application/json" or content.lstrip().startswith((b"{", b"[")):
        raise ValueError("Hydra returned JSON instead of an NZB (check the download link and Hydra API access)")
    raise ValueError(f"Hydra returned an unsupported download format ({mime}); expected NZB/XML, gzip or ZIP")


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
                broad_search INTEGER NOT NULL DEFAULT 1, resolution_mode TEXT NOT NULL DEFAULT 'inherit',
                last_checked TEXT, next_run TEXT, last_error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY, watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
                title TEXT NOT NULL, fingerprint TEXT NOT NULL, size_bytes INTEGER, resolution TEXT,
                published TEXT, link TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'found',
                nzo_id TEXT, note TEXT, found_at TEXT NOT NULL, queued_at TEXT, finished_at TEXT,
                UNIQUE(watch_id, fingerprint)
            );
            CREATE INDEX IF NOT EXISTS matches_nzo ON matches(nzo_id);
            CREATE TABLE IF NOT EXISTS grabs (
                fingerprint TEXT PRIMARY KEY, match_id INTEGER NOT NULL,
                nzo_id TEXT NOT NULL, queued_at TEXT NOT NULL, content_key TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, type TEXT NOT NULL, message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """)
            # Online migrations for installations already running v0.1.0.
            watch_columns = {r[1] for r in db.execute("PRAGMA table_info(watches)")}
            if "broad_search" not in watch_columns:
                db.execute("ALTER TABLE watches ADD COLUMN broad_search INTEGER NOT NULL DEFAULT 0")
            if "resolution_mode" not in watch_columns:
                db.execute("ALTER TABLE watches ADD COLUMN resolution_mode TEXT NOT NULL DEFAULT 'inherit'")
            match_columns = {r[1] for r in db.execute("PRAGMA table_info(matches)")}
            if "resolution" not in match_columns:
                db.execute("ALTER TABLE matches ADD COLUMN resolution TEXT")
            if "sab_missing_since" not in match_columns:
                db.execute("ALTER TABLE matches ADD COLUMN sab_missing_since TEXT")
            grab_columns = {r[1] for r in db.execute("PRAGMA table_info(grabs)")}
            if "content_key" not in grab_columns:
                db.execute("ALTER TABLE grabs ADD COLUMN content_key TEXT")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS grabs_content_key ON grabs(content_key)")

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
        columns = ["name", "query", "enabled", "mode", "match_mode", "required_words", "excluded_words", "categories", "sab_category", "min_mb", "max_mb", "max_age_days", "max_grabs", "poll_minutes", "broad_search", "resolution_mode"]
        vals = [data.get(c, {"broad_search": True, "resolution_mode": "inherit"}.get(c)) for c in columns]
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
            matches = self.rows(db.execute("SELECT m.id,m.watch_id,w.name AS watch_name,m.title,m.size_bytes,m.resolution,m.published,m.status,m.nzo_id,m.note,m.found_at,m.queued_at,m.finished_at FROM matches m JOIN watches w ON w.id=m.watch_id ORDER BY m.id DESC LIMIT 250"))
            events = self.rows(db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 80"))
            counts = dict(db.execute("SELECT (SELECT count(*) FROM watches) watches, (SELECT count(*) FROM watches WHERE enabled=1) active, (SELECT count(*) FROM matches WHERE status='found') found, (SELECT count(*) FROM matches WHERE status='queued') queued, (SELECT count(*) FROM matches WHERE status IN ('completed','assumed_completed')) completed").fetchone())
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
        params = {"t": "search", "q": search_query(watch, self.config), "apikey": self.config["hydra_key"], "limit": 100}
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
            log.warning("Pushover failed: %s", safe_error(ex))
            self.store.log("warning", f"Pushover notification failed: {safe_error(ex)}")

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
                    minutes = watch["poll_minutes"] or self.config.get("scan_interval_minutes", 30)
                    db.execute("UPDATE watches SET next_run=?,last_checked=?,last_error=NULL WHERE id=?", (iso_after(minutes), now(), wid))
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
        effective = {**watch, "query": search_query(watch, self.config)}
        for r in results:
            r["resolution"] = parse_resolution(r["title"])
        eligible = [r for r in results if matching(effective, r) and quality_allowed(watch, r, self.config)]
        quality_on = self.config.get("resolution_enabled", False) if watch.get("resolution_mode", "inherit") == "inherit" else watch.get("resolution_mode") == "on"
        if quality_on:
            eligible.sort(key=lambda r: quality_sort_key(r, self.config))
        else:
            eligible.sort(key=lambda r: r.get("published") or "", reverse=True)
        # Never attempt an unsafe empty/broad query.
        clean_tokens = normalize_title(effective["query"]).split()
        if (len("".join(clean_tokens)) < 3 or
            (watch["mode"] == "auto" and len(clean_tokens) < 2 and len(clean_tokens[0]) < 10)):
            raise ValueError("Search query is too broad for automatic downloading; add more identifying words or use notify-only")
        new_count = 0
        pushed = 0
        queued_this_scan = 0
        for r in eligible:
            fp = fingerprint(r["title"])
            with self.store.conn() as db:
                existing = db.execute("SELECT id FROM matches WHERE watch_id=? AND fingerprint=?", (watch["id"], fp)).fetchone()
                if existing:
                    mid = existing[0]
                    # A previously stored <link> may be a details page instead of
                    # the NZB enclosure. Refresh only retryable matches so a
                    # re-search repairs old failures without affecting queued jobs.
                    db.execute("UPDATE matches SET link=? WHERE id=? AND status IN ('found','failed') AND link<>?", (r["link"], mid, r["link"]))
                else:
                    c = db.execute("INSERT INTO matches(watch_id,title,fingerprint,size_bytes,resolution,published,link,status,found_at) VALUES(?,?,?,?,?,?,?,?,?)", (watch["id"], r["title"], fp, r["size_bytes"], r["resolution"], r["published"], r["link"], "found", now()))
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
            key = content_fingerprint(m["title"]) if self.config.get("dedupe_across_resolutions", True) else None
            globally = db.execute("SELECT nzo_id FROM grabs WHERE fingerprint=? OR (content_key IS NOT NULL AND content_key=?)", (m["fingerprint"], key)).fetchone()
        if globally:
            with self.store.conn() as db:
                db.execute("UPDATE matches SET status='skipped',note=? WHERE id=?", ("Already queued for another watch (SAB ID: " + globally[0] + ")", mid))
            return "duplicate"
        if w["max_grabs"] != 0 and used >= w["max_grabs"]:
            return "limit"
        try:
            if not self.config["sab_url"] or not self.config["sab_key"]:
                raise RuntimeError("SABnzbd settings are missing")
            # Always use the current Hydra API key, even if the RSS download
            # URL already contains a blank, expired or incorrect key.
            url = authenticated_hydra_url(self.config["hydra_url"], m["link"], self.config["hydra_key"])
            # Hydra may proxy NZBs directly OR reply with a redirect to its indexer.
            # If it redirects, let SABnzbd request the authenticated Hydra URL itself.
            # Both Hydra and SAB must then be network-reachable from SAB's container.
            async with self.client.stream("GET", url) as r:
                content_type = r.headers.get("content-type", "")
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
                # Forward only the authenticated Hydra URL, never a foreign
                # indexer redirect (and never a stale RSS-provided API key).
                sab_params["mode"] = "addurl"
                sab_params["name"] = url
                res = await self.client.post(self.config["sab_url"] + "/api", params=sab_params)
            else:
                filename, mime = nzb_upload_info(content, content_type, secrets=(self.config["hydra_key"], self.config["sab_key"]))
                sab_params["mode"] = "addfile"
                res = await self.client.post(self.config["sab_url"] + "/api", params=sab_params, files={"nzbfile": (filename, content, mime)})
            res.raise_for_status()
            data = res.json()
            ids = data.get("nzo_ids") or []
            if not data.get("status") or not ids:
                raise ValueError(f"SABnzbd rejected file: {data.get('error', 'unknown error')}")
            nzo_id = str(ids[0])
            with self.store.conn() as db:
                # Keep a global unique title fingerprint across watch rules.
                db.execute("INSERT INTO grabs(fingerprint,match_id,nzo_id,queued_at,content_key) VALUES(?,?,?,?,?)", (m["fingerprint"], mid, nzo_id, now(), key))
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
        """Reconcile queued NZBWatch jobs against SAB's queue and history.

        Prefer explicit Completed / Failed history records. Optionally, after
        successful API checks and a persisted grace period, classify a job missing
        from both queue and history as *assumed* complete (not verified).
        """
        if not self.config["sab_url"] or not self.config["sab_key"]:
            return
        with self.store.conn() as db:
            rows = self.store.rows(db.execute(
                "SELECT id,nzo_id,title,note,sab_missing_since FROM matches "
                "WHERE status='queued' AND nzo_id IS NOT NULL "
                "ORDER BY id DESC LIMIT 1000"
            ))
        if not rows:
            return

        def get_slots(response: dict, section: str) -> list[dict]:
            # Malformed API responses must NOT be interpreted as an empty queue.
            value = response.get(section)
            if not isinstance(value, dict) or not isinstance(value.get("slots"), list):
                raise ValueError(f"Unexpected SABnzbd {section} response; status not changed")
            return [slot for slot in value["slots"] if isinstance(slot, dict)]

        def index_slots(slots: list[dict]) -> dict[str, dict]:
            return {str(slot["nzo_id"]): slot for slot in slots if slot.get("nzo_id")}

        # Limit pagination to bound API cost. An absent job is classified only
        # by the explicit, configurable grace-period policy below.
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
                                "UPDATE matches SET status=?,finished_at=?,note=?,sab_missing_since=NULL "
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
                        # Cancel any absence timer when the job reappears.
                        if row["sab_missing_since"] or (row["note"] and row["note"].startswith("SAB status unverified:")):
                            with self.store.conn() as db:
                                db.execute("UPDATE matches SET note=NULL,sab_missing_since=NULL WHERE id=? AND status='queued'", (row["id"],))
                        continue

                    # A successful enqueue is not proof of successful completion.
                    # Start a *persisted* timer on the first verified absence,
                    # rather than using queued_at, which might be weeks old.
                    missing_since = row["sab_missing_since"]
                    if not missing_since:
                        missing_since = now()
                        with self.store.conn() as db:
                            db.execute("UPDATE matches SET sab_missing_since=? WHERE id=? AND status='queued'", (missing_since, row["id"]))

                    if self.config.get("sab_missing_policy", "keep_queued") == "assume_completed":
                        try:
                            absent_for = datetime.now(UTC) - datetime.fromisoformat(missing_since)
                            grace = timedelta(minutes=self.config.get("sab_missing_grace_minutes", 60))
                        except (ValueError, TypeError, OverflowError):
                            absent_for = timedelta(0)
                            grace = timedelta(minutes=60)
                        if absent_for >= grace:
                            note = ("Completion assumed: SABnzbd accepted the job but its ID "
                                    "is no longer present in queue or available history. "
                                    "Completion was not verified; job may have been deleted or failed.")
                            with self.store.conn() as db:
                                updated = db.execute(
                                    "UPDATE matches SET status='assumed_completed',finished_at=?,note=? "
                                    "WHERE id=? AND status='queued'",
                                    (now(), note, row["id"])
                                ).rowcount
                            if updated:
                                self.store.log("assumed_completed", f"{row['title']}: Assumed complete (not verified by SABnzbd)")
                                # Do NOT send a confirmed-completion notification.
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
