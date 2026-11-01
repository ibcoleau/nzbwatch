name: Publish Docker Hub image

on:
  push:
    tags:
      - 'v[0-9]*'

permissions:
  contents: read

jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-python@v6
        with:
          python-version: '3.12'
          cache: pip
      - name: Verify tag matches VERSION
        run: test "v$(cat VERSION)" = "$GITHUB_REF_NAME"
      - name: Install dependencies
        run: python -m pip install -r requirements.txt pytest
      - name: Run tests
        run: python -m pytest -q
      - name: Set up QEMU
        uses: docker/setup-qemu-action@v4
      - name: Set up Buildx
        uses: docker/setup-buildx-action@v4
      - name: Login to Docker Hub
        uses: docker/login-action@v4
        with:
          username: ${{ vars.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_TOKEN }}
      - name: Docker metadata
        uses: docker/metadata-action@v6
        id: meta
        with:
          images: ${{ vars.DOCKERHUB_USERNAME }}/nzbwatch
          flavor: |
            latest=false
          tags: |
            type=semver,pattern={{version}}
            type=sha
            type=raw,value=latest,enable=${{ !contains(github.ref_name, '-') }}
      - name: Build and publish
        uses: docker/build-push-action@v7
        with:
          context: .
          platforms: linux/amd64,linux/arm64
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
