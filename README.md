# NZBWatch v0.2.7 — SABnzbd auto-cleanup handling

This is an **incremental patch**, not a replacement of your existing source. It accepts NZBWatch source v0.2.5 or v0.2.6 and preserves unrelated changes. If your checkout is v0.2.5, the installer applies the v0.2.6 SAB queue/history reconciliation patch first.

## What changes

- Adds a **SABnzbd status reconciliation** panel to **Settings**.
- **Keep as Queued** is the conservative default; optional **Assume complete after grace period** handles installations that automatically purge successful SAB jobs from both queue and history.
- Configurable **absence grace period**, default 60 minutes (5–10080 minutes). It begins **when NZBWatch first observes the item missing**, not from its original submission time.
- Remembers the first absence in a new SQLite column `matches.sab_missing_since` so it survives restarts.
- Before any assumption, NZBWatch checks SAB's current queue, recent history and archived history. If a job reappears, the missing timer resets. A confirmed SAB `Completed` or `Failed` takes priority.
- Missing jobs are labelled **Assumed complete** (`assumed_completed` in SQLite), distinct from verified `Completed`, and show a note explaining that success was not verified. Both appear in the Overview completed count; the Activity status filter can distinguish them.
- Assumed completions **do not send a Pushover success notification** (they are not proof of success).
- API failures or unexpected/malformed responses do not start an absence timer or trigger inferred completion. Jobs are never requeued by this reconciliation logic.

**Important:** Disappearance alone does not mean success. A job could have been deleted, cleaned up after a failed download, or purged from history. This optional policy is suitable only if you trust your SAB cleanup process. For definitive status, retain SAB history or configure a completion callback/script in SABnzbd.

## Apply to GitHub source

1. Back up the Unraid appdata folder `/mnt/user/appdata/nzbwatch` (SQLite DB and `config.json`).
2. Extract this ZIP on your Windows development computer.
3. Open a terminal in your existing `ibcoleau/nzbwatch` Git checkout, with its `VERSION` and `app` folder.
4. Execute:

   ```powershell
   python "C:\\path\\to\\extracted-patch\\apply_patch.py" .
   python -m pytest -q
   git status
   ```
5. Commit and push to `main`:

   ```powershell
   git add app/core.py app/main.py app/settings.py app/static/app.js app/static/index.html app/static/style.css VERSION tests
   git commit -m "Handle missing SABnzbd jobs with optional assumed completion"
   git push origin main
   git tag v0.2.7
   git push origin v0.2.7
   ```
6. When GitHub Actions publishes the image, change Unraid's Repository field to `virtike/nzbwatch:0.2.7` and click Apply. Keep your existing `/data` mapping and environment credentials unchanged.
7. In **NZBWatch → Settings → SABnzbd status reconciliation**, select **Mark as Assumed complete after grace period**, set the grace period (e.g. 60 minutes), and **Save settings**. This setting persists to `/data/config.json` and takes effect without restarting the container.

## Testing and limitations

49 automated tests passed on the local patched project, including seven new tests covering missing-job grace, persistence, reappearance, API errors, explicit failures and the disabled-policy case. Source patch was also tested on a v0.2.5-like checkout and on the v0.2.6 local project. Not tested against your live SABnzbd or actual Unraid environment.

Do not apply this patch to an unrelated fork or source version; it validates expected source anchors and stops if incompatible, instead of overwriting code silently.
