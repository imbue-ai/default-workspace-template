# Separate verifier environment: a slim python image with uv, holding the
# rewardkit criteria. In separate mode this directory (tests/) is the build
# context and the image must provide /tests/test.sh itself -- harbor does not
# upload tests/ at runtime.
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /usr/local/bin/

COPY . /tests
RUN chmod +x /tests/test.sh
