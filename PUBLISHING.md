# Publish NZBWatch to GitHub and Docker Hub

This version adds two GitHub Actions workflows:
- `.github/workflows/ci.yml`: tests every push and pull request to `main`.
- `.github/workflows/publish.yml`: when a version tag is pushed, checks `VERSION`, runs tests, builds AMD64 and ARM64 images and publishes to Docker Hub. Stable releases get `latest` and versioned tags; prereleases won't overwrite `latest`.

## Setup

1. Create an empty GitHub repository named `nzbwatch` (start private if unsure; no auto-generated README).
2. Create the Docker Hub image repository `nzbwatch` in your Docker Hub account.
3. Generate a Docker Hub Personal Access Token with write permission.
4. In GitHub repository **Settings > Secrets and variables > Actions** add:
   - **Variable** `DOCKERHUB_USERNAME`: Docker Hub username.
   - **Secret** `DOCKERHUB_TOKEN`: Docker Hub token (not your account password).
5. Commit all files in this directory, **not** the parent directory:

```bash
git init
git add .
git commit -m "Initial NZBWatch"
git branch -M main
git remote add origin https://github.com/YOUR_GITHUB_USERNAME/nzbwatch.git
git push -u origin main
```

6. Wait until the Tests workflow passes. Create a version tag matching `VERSION` (currently `0.1.0`):

```bash
git tag v0.1.0
git push origin v0.1.0
```

The Publish workflow will run tests, build the image, and publish `yourusername/nzbwatch:0.1.0`, `:latest`, and an immutable SHA-derived tag. To release a future version, edit `VERSION`, commit/push changes, then tag the matching version.

## Deployment

Create `.env` from `.env.example`, configure credentials for the app and APIs, and add:

```dotenv
DOCKER_IMAGE=YOUR_DOCKERHUB_USERNAME/nzbwatch:0.1.0
```

Deploy the published image, without rebuilding:

```bash
docker compose -f compose.deploy.yaml pull
docker compose -f compose.deploy.yaml up -d
```

`compose.yaml` is still available for local development (`docker compose up -d --build`). Both files use the same named data volume, if invoked from the same Compose project directory.

## Security / release notes

Do not commit `.env`, `.sqlite3` files, API keys, Pushover credentials, or database data. Keep GitHub repo and Docker Hub visibility private until you are happy sharing the code/image. Review old commits before changing an existing repository from private to public. Pin servers to versioned tags instead of `latest` for predictable upgrades. GitHub Actions runners must be enabled for your repository. The release workflow has not been executed against your own GitHub/Docker Hub accounts yet.
