# NZBWatch v0.2.1

**Updated 2 November 2026.** Hotfix for NZBHydra2 download URLs and NZB payload detection, retaining web-managed settings and quality preferences.


A self-hosted, password-protected NZBHydra2 watchlist and SABnzbd automation dashboard. Includes Pushover notifications. Designed for a **single Docker container and a persistent Docker volume**; no Node, Redis, or external database is needed.

## Features

- Edit/search/enable/disable/delete watch rules in the browser.
- Newznab-compatible NZBHydra2 search, using the documented `/api?t=search` interface.
- Per-watch search rules, phrase filters, categories, size/age limits, auto-download/notify-only, maximum grabs, optional per-watch interval or inherited global interval, broad-search control and resolution-filtering override.
- Editable **Settings** page for Hydra/SABnzbd URLs and API keys, Pushover token/user, notification events, global check interval, max queues, search cleaning, resolution priority and fallback minimum. Saved to `/data/config.json` and applied live without restarting.
- Resolution parser recognises `1080p`, `2160p`, `4K`, `UHD`, `720p`, `480p`, `576p`, `360p` and common pixel dimensions. Candidate results are ranked by preferred resolution before queuing.
- Broad search normalization strips trailing group identifiers and technical release suffixes. Local candidate matching still checks the cleaned title tokens.
- NZB submission to SABnzbd. Prefer Newznab RSS enclosure URLs over detail-page links. Accept genuine NZB XML and compressed NZB archives via `addfile`; on Hydra redirects use SAB `addurl` pointed back at Hydra. HTML/login or JSON error pages are rejected with an actionable error instead of misleading "did not return NZB" messages.
- Track SABnzbd queue IDs and check SAB history for **Completed** / **Failed** status.
- Pushover notifications for `found`, `queued`, `completed`, and `failed` events. Defaults to `found,completed,failed`; customize in Settings.
- SQLite persistence with separate watch rules, matched results, submitted titles, and a rolling event log. Watch rules export/import as JSON.
- Duplicate prevention by normalized release title, plus optional (enabled by default) deduplication of video releases across different resolutions and watch rules using cleaned content identity.
- HTTPS-ready cookie sessions, CSRF on writes, in-memory login throttling, no API keys exposed to browsers, and no external frontend dependencies.
- One-off watches **automatically pause** after reaching their configured number of successfully queued items. When `max_grabs=0`, the watch stays enabled. A per-scan auto-queue cap of 3 avoids bulk additions from broad searches (configurable in Settings).

## v0.2.1 download hotfix

- NZBHydra's `<enclosure url="...">` is now preferred over an RSS `<link>` that may refer to an HTML/details page.
- Hydra payload validation now recognises XML NZBs with long declarations/comments, plus gzip, ZIP and RAR archives accepted by SABnzbd.
- Unexpected HTML, XML error/RSS, and JSON responses are diagnosed without exposing the response body or API keys.
- Failed or found matches refresh their stored download URLs on the next search; queued/completed matches are not altered. You can retry a failed item after a scan instead of deleting watch history.
- If Hydra redirects to an indexer, NZBWatch continues forwarding its authenticated Hydra URL to SABnzbd (`addurl`). SAB must be able to reach Hydra for this case.

## Upgrading an existing Unraid container

See **[UPGRADE_UNRAID.md](UPGRADE_UNRAID.md)**. You must retain the existing `/data` mapping, and update the container image to `virtike/nzbwatch:0.2.1` after publishing the `v0.2.1` Git tag. Take an appdata backup before upgrading. The SQLite database is migrated in place and secrets are imported from existing env vars on first startup.

## Quick start (Docker Compose)

1. Extract this project to a folder on your Docker host, such as `/mnt/user/appdata/nzbwatch/` on Unraid.
2. Copy `.env.example` to `.env`.
3. Edit `.env` and fill in **strong, unique** `ADMIN_PASSWORD` (at least 12 characters) and `APP_SECRET` (at least 32 characters). Generate a secret:

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

4. **Optional first-boot migration:** Set `HYDRA_URL`, `HYDRA_API_KEY`, `SAB_URL`, and `SAB_API_KEY` in `.env`, or leave them blank and enter them in the web UI. On first boot the app writes its current integration settings to `/data/config.json`. Once this file exists, it takes precedence over old env values.
5. From the extracted project directory run:

   ```bash
   docker compose up -d --build
   ```

6. Visit **http://YOUR-DOCKER-HOST-IP:8787** and sign in using the password in `.env`. Use **Settings** to edit/test integration with Hydra, SABnzbd, and Pushover. Add a watch and use **Check now**.

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

**One-shot item**: Search `An.Example.Name.2026.1080p.WEB-DL-GROUP`, choose *All words*, *Auto-download*, `max_grabs=1` and optional age/size filters. With broad search enabled it queries `An Example Name 2026`. If quality filtering is enabled, it chooses the best eligible release according to Settings, queues it, then pauses the watch.

**Notify-only**: Search `Unusual Rare Album`, choose *Notify only*, and leave the watch enabled. Matches appear under Activity with a manual **Queue** button. Configure Pushover to receive notifications.

**Recurring releases**: Set `max_grabs=0` for unlimited matches and choose a very specific query and suitable include/exclude filters. Automatic submission is capped by `MAX_QUEUES_PER_SCAN` per scan; use care with broad searches.

The search endpoint returns up to **100** results per poll, so unusually broad searches may not find results beyond that page. Filters (required/excluded/size) are applied *after* Hydra returns results. Release dates without a parseable timestamp are not excluded by a max-age filter.

### Matching semantics

- **All words**: every word from the *cleaned search query* must appear as a distinct normalized token in a result title.
- **Exact word sequence**: the cleaned normalized query phrase must appear in the normalized title.
- **Any word**: any word from the query matches locally; NZBHydra2 receives the cleaned query when broad search is enabled.
- **Required/excluded phrases**: comma-separated values, case-insensitive, applied to release titles after punctuation normalization.
- Results deduplicate by normalized *title*, not by indexer or Hydra GUID, to avoid repeat downloads of the same title. Two distinct files with the same normalized title are treated as the same release.

## Security and deployment

- `ADMIN_PASSWORD`, `APP_SECRET` and `COOKIE_SECURE` remain Docker environment settings in this release. Changing those still requires recreating the container. Hydra, SABnzbd, Pushover and scan/quality settings use `/data/config.json` and update live.
- **Do not commit `.env` or `/data/config.json` to Git.** `config.json` contains API keys as plaintext with file permissions `0600`; encryption at rest is not provided. Protect and restrict access to your Unraid appdata backups. The browser never receives stored keys; leaving a key field blank retains it, and Clear explicitly removes it.
- Change `APP_SECRET` to revoke all signed sessions.
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

- This is a **v0.2.0**, tested with mocked Hydra/SABnzbd responses and HTTP authentication flows. Live end-to-end connectivity to your actual Hydra, SABnzbd and Pushover installations has not been tested here.
- `nzo_id` tracking depends on a job remaining visible in SABnzbd history. If history is purged before the next check, NZBWatch cannot verify completion and the match stays in `queued`.
- There is one admin account, not role-based multi-user permissions.
- JSON export/import covers watch rules only. For a full backup, back up the SQLite Docker volume.
- No automatic upgrades, notification retry queue, or webhook callbacks yet. The app polls download history every 30 seconds and searches at each watch's interval or the inherited global interval.
- Resolution is inferred from release titles, not verified media metadata. It may be incorrect if a title is misleading. Searches still examine at most the first 100 Hydra results.
- The quality fallback is **immediate**, not delayed: if the first scan has only an acceptable lower-quality result, it can queue it and stop the one-off watch. The app does not wait for preferred resolution to appear.
- Previous watches keep their previously configured per-watch intervals and **broad search is disabled** for those older watches to avoid changing their results unexpectedly. Edit a watch to enable broad searching, and set its interval to **0** to inherit the global interval.
- Global content deduplication is enabled by default. For recognized video resolutions it uses the normalized release identity before technical quality tags. Disable it in Settings if you intentionally download multiple resolutions of the same title. Existing v0.1 queued records are not retroactively assigned content keys.

### Relevant API documentation

- NZBHydra2: https://github.com/theotherp/nzbhydra2/wiki/External-API,-RSS-and-cached-queries
- SABnzbd: https://sabnzbd.org/wiki/advanced/api
- Pushover: https://pushover.net/api

## Publishing to GitHub / Docker Hub

See [PUBLISHING.md](PUBLISHING.md) for setting up the GitHub repository,
Docker Hub access token, automated test and release workflows, and the
`compose.deploy.yaml` file for deploying published images without building locally.
