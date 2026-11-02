# Upgrading NZBWatch on Unraid to v0.2.7

1. **Back up appdata first**: the existing `/mnt/user/appdata/nzbwatch` directory contains your SQLite database and `/data/config.json`. For a consistent database backup, stop the container before copying the database/WAL/SHM files (or use SQLite's backup API while running).
2. Publish the full application to `https://github.com/ibcoleau/nzbwatch` and push tag `v0.2.7` as described in [PUBLISHING.md](PUBLISHING.md).
3. Verify `virtike/nzbwatch:0.2.7` is published to Docker Hub. Merely creating this ZIP does **not** publish the image.
4. In **Unraid → Docker → NZBWatch → Edit**, change only the Repository image to **`virtike/nzbwatch:0.2.7`**. Keep the existing `8787:8787` port mapping and **container `/data` → host `/mnt/user/appdata/nzbwatch`** path mapping.
5. Keep your existing `ADMIN_PASSWORD` and `APP_SECRET` variables (and any other variables). The existing `/data/config.json` takes precedence over service-related environment variables. Click **Apply** to pull and recreate the image.
6. Log into NZBWatch and go to **Settings → SABnzbd status reconciliation**. To account for automatic SAB history cleanup, choose **Assume complete after grace period**, set the grace period (default 60 minutes), and save. The setting is live, without a second container restart.
7. Observe Activity: verified SAB history successes show **Completed**, jobs confirmed failed show **Download failed**, and previously submitted jobs absent from all accessible SAB records long enough show **Assumed complete**. The latter is *unverified* and intentionally does not send a confirmed-success notification.

## Data migration / previous watches

SQLite automatically adds the `sab_missing_since` column if absent; timestamps are persisted in existing `/data` and survive container restarts. Existing watches, grab counts, completed/paused sections, layout preferences, and configuration are preserved. Missing-job assumptions begin when the app **first observes absence**, not retroactively from when a job was queued.

## Rollback

If required, revert the image repository to the previous tag. Retain the pre-upgrade appdata backup, since older versions may not understand newer statuses or database fields. Do not delete the `/data` volume or appdata directory when upgrading.

## Security

`/data/config.json` contains service API keys as plaintext with restrictive permissions. Keep it out of GitHub and protect backups. Administrator login credentials and the session secret remain Docker environment settings; changing those requires recreating the container.
