- CI's pytest steps no longer set `PYTEST_MAX_DURATION_SECONDS`: nothing in this repo reads
  it now that the apps no longer register mngr-internal's conftest hooks. The root
  `pyproject.toml` declares the `flaky` marker beside `acceptance` and `release`.
