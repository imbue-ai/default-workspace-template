# Separate verifier environment: a slim python image with uv, holding the
# rewardkit criteria. In separate mode this directory is the build context and
# the image must provide /tests/test.sh itself -- harbor does not upload the
# tests directory at runtime.
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /usr/local/bin/

# The criteria first and the case data last. case.json is the one file that differs between two
# tasks in a dataset, and between two steps of a stepped task (each of which builds its own verifier
# image from its own copy of this context), so copying it last lets every one of those builds reuse
# the criteria layer beneath it. Nothing case-specific may be added after this COPY, or it is
# rebuilt per case and per step. The criteria layer itself is shared by every task declaring the
# same set of scoring dimensions: a case with expectations carries an outcome/ directory and a case
# without it does not.
COPY verifier /tests
RUN chmod +x /tests/test.sh
COPY case.json /tests/case.json
