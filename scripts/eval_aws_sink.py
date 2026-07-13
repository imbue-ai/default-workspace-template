"""S3 sink for the eval worker.

- restic snapshots of /mngr (tagged post_message_<k>), using the create-menu-configured
  runtime/secrets/restic.env (repo + AWS creds + password). We invoke restic ourselves at the
  turns we choose -- the host-backup service is stopped so nothing runs on a hidden cadence.
- plain S3 objects (state.json, transcript, artifacts) via boto3, reusing the SAME AWS creds from
  restic.env, into the case's S3 prefix (passed in config.json).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

HOST_DIR = os.environ.get("MNGR_HOST_DIR", "/mngr")

# Match host-backup's exclude set so snapshots stay lean (deps are reinstallable from lockfiles).
_RESTIC_EXCLUDES = (
    "--exclude=**/.venv", "--exclude=**/node_modules", "--exclude=**/__pycache__",
    "--exclude=**/.pytest_cache", "--exclude=**/.ruff_cache", "--exclude=**/target",
    "--exclude=**/dist", "--exclude=**/build", "--exclude=**/.next", "--exclude=**/.cache",
)


def _load_restic_env() -> dict:
    """Read runtime/secrets/restic.env (reuse FCT's loader when importable)."""
    try:
        from host_backup.config import load_restic_env  # type: ignore

        return dict(load_restic_env())
    except Exception:
        env: dict[str, str] = {}
        path = Path("runtime/secrets/restic.env")
        if path.is_file():
            for raw in path.read_text().splitlines():
                line = raw.strip()
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env[key.strip()] = value.strip().strip("'\"")
        return env


class AwsSink:
    def __init__(self, config: dict):
        self._config = config
        self._restic_env = _load_restic_env()
        self._bucket = config["s3_bucket"]
        self._prefix = str(config["s3_prefix"]).rstrip("/")
        import boto3

        self._s3 = boto3.client(
            "s3",
            aws_access_key_id=self._restic_env.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=self._restic_env.get("AWS_SECRET_ACCESS_KEY"),
            region_name=self._restic_env.get("AWS_DEFAULT_REGION", "us-east-1"),
        )

    def stop_host_backup(self) -> None:
        """Take over snapshot cadence for THIS sandbox only (config unchanged; non-eval unaffected)."""
        subprocess.run(["supervisorctl", "stop", "host-backup"], capture_output=True, text=True)

    def _restic_configured(self) -> bool:
        return bool(self._restic_env.get("RESTIC_REPOSITORY") and self._restic_env.get("RESTIC_PASSWORD"))

    def restic_snapshot(self, tag: str) -> None:
        if not self._restic_configured():
            print("[eval] restic.env not configured -- skipping snapshot", tag, flush=True)
            return
        env = {**os.environ, **self._restic_env}
        if subprocess.run(["restic", "cat", "config"], env=env, capture_output=True, text=True).returncode != 0:
            subprocess.run(["restic", "init"], env=env, capture_output=True, text=True)
        result = subprocess.run(
            ["restic", "backup", HOST_DIR, "--tag", tag, *_RESTIC_EXCLUDES],
            env=env, capture_output=True, text=True,
        )
        print("[eval] restic snapshot {} rc={}".format(tag, result.returncode), flush=True)

    def _put(self, key: str, data: bytes, content_type: str) -> None:
        self._s3.put_object(Bucket=self._bucket, Key="{}/{}".format(self._prefix, key), Body=data, ContentType=content_type)

    def write_state(self, waits_done: int, num_turns: int, test_state: str) -> None:
        payload = {
            "eval_name": self._config.get("eval_name"),
            "case_name": self._config.get("case_name"),
            "waits_done": waits_done,
            "num_turns": num_turns,
            "test_state": test_state,
        }
        self._put("state.json", json.dumps(payload, indent=2).encode("utf-8"), "application/json")

    def upload_transcript(self, events: list[dict]) -> None:
        body = "\n".join(json.dumps(event) for event in events).encode("utf-8")
        self._put("artifacts/full_transcript.jsonl", body, "application/x-ndjson")
