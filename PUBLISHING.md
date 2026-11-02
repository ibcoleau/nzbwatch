# NZBWatch v0.2.7 — publish a complete application release

This ZIP contains the **full application**, not an incremental patch. It includes `app/`, tests, Dockerfile, Compose definitions, `.github/workflows/`, and `VERSION=0.2.7`.

## Publish to the existing repositories

1. Clone your existing GitHub repository if you do not have a local checkout:

   ```bash
   git clone https://github.com/ibcoleau/nzbwatch.git
   cd nzbwatch
   ```

2. Back up your local changes or commit them first. Copy the **contents of this ZIP** into the repository root, replacing the corresponding tracked files. Keep `.git` intact. **Do not copy `.env`, `/data`, appdata or live `config.json` into Git.**
3. Run tests:

   ```bash
   python -m pip install -r requirements.txt pytest
   python -m pytest -q
   ```

4. Commit the application and publish the new tag:

   ```bash
   git add .
   git commit -m "NZBWatch v0.2.7: handle SAB auto-cleanup"
   git push origin main
   git tag v0.2.7
   git push origin v0.2.7
   ```

   The publishing workflow verifies that Git tag `v0.2.7` matches the `VERSION` file. Do not recreate or force-move an existing release tag; use a newer patch version if it already exists.

5. Confirm **GitHub Actions → Publish Docker Hub image** succeeds and `virtike/nzbwatch:0.2.7` appears on Docker Hub. The workflow expects `DOCKERHUB_USERNAME=virtike` in repository Actions variables and `DOCKERHUB_TOKEN` in repository Actions secrets, or a job-level environment corresponding to where they are stored.
6. Follow [UPGRADE_UNRAID.md](UPGRADE_UNRAID.md) to deploy the image while preserving `/data`.

To run the image using Compose instead of the Unraid UI, set `DOCKER_IMAGE=virtike/nzbwatch:0.2.7` in `.env` then run `docker compose -f compose.deploy.yaml up -d`. Note Compose's named volume is not the same as the Unraid `/mnt/user/appdata/nzbwatch` bind mount; keep the same existing mount when upgrading an existing installation.

## Testing scope

The project passed 49 automated Python tests at packaging time, including tests for missing SAB records, grace periods, API failures, state persistence, and reappearance. Not tested with the user's live SABnzbd/NZBHydra2 services or a real Docker build.
