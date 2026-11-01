# NZBWatch

A self-hosted, password-protected NZBHydra2 watchlist and SABnzbd automation dashboard. Includes Pushover notifications. Designed for a **single Docker container and a persistent Docker volume**; no Node, Redis, or external database is needed.

## Features

- Edit/search/enable/disable/delete watch rules in the browser.
- Newznab-compatible NZBHydra2 search, using the documented `/api?t=search` interface.
- Per-watch query, name, matching mode (all words / phrase / any), required phrases, excluded phrases, Newznab category IDs, SABnzbd category, min/max size, maximum release age, search interval (minimum 5 minutes), auto-download/notify-only mode, and maximum grabs.
- NZB submission to SABnzbd. If Hydra returns file contents, use SAB `addfile`. If Hydra redirects, use SAB `addurl` pointed back at Hydra (not the external redirect URL).
- Track SABnzbd queue IDs and check SAB history for **Completed** / **Failed** status.
- Pushover notifications for `found`, `queued`, `completed`, and `failed` events. Defaults to `found,completed,failed`; turn on queued notifications via `.env`.
- SQLite persistence with separate watch rules, matched results, submitted titles, and a rolling event log. Watch rules export/import as JSON.
- Duplicate prevention by normalized release title (stable across API result ID changes). Prevents repeat queuing by another watch rule.
- HTTPS-ready cookie sessions, CSRF on writes, in-memory login throttling, no API keys exposed to browsers, and no external frontend dependencies.
- One-off watches **automatically pause** after reaching their configured number of successfully queued items. When `max_grabs=0`, the watch stays enabled. A per-scan auto-queue cap of 3 avoids bulk additions from broad searches (configurable).

## Quick start (Docker Compose)

1. Extract this project to a folder on your Docker host, such as `/mnt/user/appdata/nzbwatch/` on Unraid.
2. Copy `.env.example` to `.env`.
3. Edit `.env` and fill in **strong, unique** `ADMIN_PASSWORD` (at least 12 characters) and `APP_SECRET` (at least 32 characters). Generate a secret:

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

4. Set `HYDRA_URL`, `HYDRA_API_KEY`, `SAB_URL`, and `SAB_API_KEY` in `.env`. Supply optional Pushover user key and app token.
5. From the extracted project directory run:

   ```bash
   docker compose up -d --build
   ```

6. Visit **http://YOUR-DOCKER-HOST-IP:8787** and sign in using the password in `.env`. Use **Connections** to test integration with Hydra, SABnzbd, and Pushover. Add a watch and use **Check now**.

To see logs:

```bash
docker compose logs -f nzbwatch
```

To rebuild after changing application files:

```bash
docker compose up -d --build
```

Data is persisted in the named Docker volume `nzbwatch-data` (usually named `projectname_nzbwatch-data` by Compose). **Never run `docker compose down -v` unless intentionally deleting all watch history.** Keep a backup of the Docker volume. The JSON export is *watch rules only*, not history.

## Networking: important

All configured service URLs are used **by the NZBWatch container**, not by the web browser. Do not use `localhost:5076` or `localhost:8080` unless those services are actually inside the same container (normally they are not). Use reachable LAN IPs (`http://192.168.x.x:5076`) or Docker DNS container names on a *shared network*. On Unraid, LAN IPs are usually the simplest starting point. Ensure SABnzbd allows API access from NZBWatch.

If Hydra redirects an NZB download to its indexer, NZBWatch submits an authenticated Hydra URL to SABnzbd using `addurl`. In that case **SABnzbd must also be able to reach the Hydra URL**. If Hydra responds with NZB file content, NZBWatch uploads the file directly to SAB and SAB does not need to reach Hydra.

## Watch configuration examples

**One-shot item**: Search `An Example Name 2026 1080p`, choose *All words*, set *Auto-download*, `max_grabs=1` and an age/size filter where useful. NZBWatch queues the first matching result and pauses the watch.

**Notify-only**: Search `Unusual Rare Album`, choose *Notify only*, and leave the watch enabled. Matches appear under Activity with a manual **Queue** button. Configure Pushover to receive notifications.

**Recurring releases**: Set `max_grabs=0` for unlimited matches and choose a very specific query and suitable include/exclude filters. Automatic submission is capped by `MAX_QUEUES_PER_SCAN` per scan; use care with broad searches.

The search endpoint returns up to **100** results per poll, so unusually broad searches may not find results beyond that page. Filters (required/excluded/size) are applied *after* Hydra returns results. Release dates without a parseable timestamp are not excluded by a max-age filter.

### Matching semantics

- **All words**: every word from `Search terms` must appear as a distinct normalized token in a result title.
- **Exact word sequence**: the normalized query phrase must appear in the normalized title.
- **Any word**: any word from the query matches locally; NZBHydra2 itself still receives the original query and may restrict its results.
- **Required/excluded phrases**: comma-separated values, case-insensitive, applied to release titles after punctuation normalization.
- Results deduplicate by normalized *title*, not by indexer or Hydra GUID, to avoid repeat downloads of the same title. Two distinct files with the same normalized title are treated as the same release.

## Security and deployment

- The admin password and API keys live in `.env` and should be protected with host filesystem permissions. **Do not commit `.env` to Git.** Change `APP_SECRET` to revoke all sessions.
- The provided Compose file binds to `0.0.0.0:8787` for convenient LAN testing. Do **not** forward this port to the public internet directly.
- For remote access, use a VPN/Tailscale or a trusted HTTPS reverse proxy with access controls. Set `APP_BIND=127.0.0.1` when the proxy runs on the same host, and `COOKIE_SECURE=true` when serving the dashboard over HTTPS.
- Run only **one container and one Uvicorn worker** against a database; the simple built-in scheduler is intended for a single process. Do not scale replicas without moving scheduling/queue coordination to a separate worker.
- Password login uses a server-signed, HttpOnly cookie and a CSRF token for state-changing requests; brute-force protection is in-memory and resets on restart. Add reverse-proxy rate-limiting if exposing remotely.
- NZB links are only fetched if they point at the configured Hydra host/port. API keys are not returned in app API responses.
- The app runs as a non-root user, with a read-only container filesystem and writable persistent `/data` volume.
- The app assumes all uses are for content you are authorized to acquire.

## Local development and tests

```bash
python3 -m venv .venv
. .venv/bin/activate               # Linux/macOS; Windows: .venv\\Scripts\\activate
pip install -r requirements.txt pytest
cp .env.example .env               # edit secrets before running
# export all variables in .env (e.g., via python-dotenv or your shell)
DATA_DIR=./data uvicorn app.main:app --host 127.0.0.1 --port 8787
```

The local Python process reads environment variables directly; the Docker Compose setup automatically loads `.env`. Tests use local SQLite and mocked APIs and **do not contact your servers**:

```bash
python -m pytest -q
```

## Current limitations / next improvements

- This is a **first usable version**, tested with mocked Hydra/SABnzbd responses and HTTP authentication flows. Live end-to-end connectivity to your actual Hydra, SABnzbd and Pushover installations has not been tested here.
- `nzo_id` tracking depends on a job remaining visible in SABnzbd history. If history is purged before the next check, NZBWatch cannot verify completion and the match stays in `queued`.
- There is one admin account, not role-based multi-user permissions.
- JSON export/import covers watch rules only. For a full backup, back up the SQLite Docker volume.
- No automatic upgrades, priority scoring, notifications retry queue, or webhook callbacks yet. The app polls for downloads every 30 seconds and searches at each watch's interval.
- Search matching considers titles and sizes, not media metadata from TMDB/TVDB or fuzzy quality rankings.

### Relevant API documentation

- NZBHydra2: https://github.com/theotherp/nzbhydra2/wiki/External-API,-RSS-and-cached-queries
- SABnzbd: https://sabnzbd.org/wiki/advanced/api
- Pushover: https://pushover.net/api
