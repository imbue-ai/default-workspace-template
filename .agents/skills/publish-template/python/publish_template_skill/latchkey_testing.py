"""A stand-in `latchkey` for the publish scripts' tests."""

import json
import os
import subprocess
import sys
from pathlib import Path

# Stands in for `latchkey curl`: answers the GitHub API and the gateway's
# permissions endpoint from a JSON state file, and logs every argv it was given.
# `grant_after` flips both kinds of access on once that many probes have run, which
# is how a test watches a wait loop see an approval land. `permissions_failure`
# ({stdout, stderr, exit}) is what the permissions endpoint answers until then.
_FAKE_LATCHKEY = """#!{python}
import json, os, sys

state = json.loads(open(os.environ["FAKE_LATCHKEY_STATE"]).read())
args = sys.argv[1:]
with open(os.environ["FAKE_LATCHKEY_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
probe_count = sum(1 for _ in open(os.environ["FAKE_LATCHKEY_LOG"]))
is_granted = state.get("grant_after") is not None and probe_count > state["grant_after"]
url = next(arg for arg in args if arg.startswith("http"))
method = "GET"
for index, arg in enumerate(args):
    if arg.startswith("-X"):
        method = arg[2:] or args[index + 1]
fail_on_http_error = bool({{"-f", "-sf", "-fs", "--fail"}} & set(args))

if url == "https://api.github.com/user":
    if state.get("api") or is_granted:
        print(json.dumps({{"login": "octo"}}))
        sys.exit(0)
    if fail_on_http_error:
        sys.exit(22)
    print(json.dumps({{"error": "request not permitted by the user"}}))
elif url == "http://latchkey-self.invalid/permissions/self":
    failure = state.get("permissions_failure")
    if failure and not is_granted:
        sys.stdout.write(failure.get("stdout", ""))
        sys.stderr.write(failure.get("stderr", ""))
        sys.exit(failure.get("exit", 0))
    rules = state.get("rules", [])
    if is_granted:
        rules = rules + [{{"github-git:octo": ["github-git-write"]}}]
    print(json.dumps({{"rules": rules}}))
else:
    print(state["responses"][method + " " + url])
"""


class FakeLatchkey:
    def __init__(self, bin_dir: Path, state_path: Path, log_path: Path) -> None:
        self.bin_dir = bin_dir
        self.state_path = state_path
        self.log_path = log_path

    def set_state(self, **state: object) -> None:
        self.state_path.write_text(json.dumps(state))

    def calls(self) -> list[list[str]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_LATCHKEY_STATE"] = str(self.state_path)
        env["FAKE_LATCHKEY_LOG"] = str(self.log_path)
        return env

    def run(self, script: Path, *args: str) -> subprocess.CompletedProcess[str]:
        """Run a publish script the way the skill does, with this stand-in on PATH."""
        return subprocess.run(
            [sys.executable, str(script), *args],
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=20,
        )


def install_fake_latchkey(tmp_path: Path) -> FakeLatchkey:
    bin_dir = tmp_path / "fake-latchkey-bin"
    bin_dir.mkdir()
    stub = bin_dir / "latchkey"
    stub.write_text(_FAKE_LATCHKEY.format(python=sys.executable))
    stub.chmod(0o755)
    fake = FakeLatchkey(
        bin_dir, tmp_path / "latchkey-state.json", tmp_path / "latchkey-calls.jsonl"
    )
    fake.set_state()
    return fake
