"""The share-gateway's one logging seam: a stderr line supervisord captures."""

import sys


def log(message: str) -> None:
    print(f"[share-gateway] {message}", file=sys.stderr, flush=True)
