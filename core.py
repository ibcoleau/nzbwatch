name: Test

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

permissions:
  contents: read

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-python@v6
        with:
          python-version: '3.12'
          cache: pip
      - name: Install dependencies
        run: python -m pip install -r requirements.txt pytest
      - name: Run tests
        run: python -m pytest -q
