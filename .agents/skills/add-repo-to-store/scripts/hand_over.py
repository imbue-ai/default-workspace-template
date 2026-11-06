"""Hand a tried install file to the App Store, which holds it for the person to confirm:
``uv run --no-sync python .agents/skills/add-repo-to-store/scripts/hand_over.py <install-file.json> --trial <dir>``
(from the workspace root).

``--trial`` is the record directory ``python -m app_store.try_install`` printed. The file is handed over only when
that trial worked and tried exactly what this file runs (the repository, the commit, the recipe and the launch) -- so
the store never asks about something nobody ran. Its words and figures may change after the trial: they are what the
trial measured. The trial's screenshot goes with it, for the confirmation to show; ``--replaces <app>`` hands it
over as an update of an app already added from GitHub. The store's address comes from the app registry, the chat is
named by ``$MINDS_CHAT_ID`` (or ``$MNGR_AGENT_ID``), and the store's answer is printed: the pending add it now shows,
or the plain reason it refused. Exits 0 only when it was handed over.
"""

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Final

import click
import httpx
from app_manifest.manifest import describe_validation_error
from app_manifest.registry import read_registry
from app_manifest.registry_location import registry_path
from app_store.repo_install_file import RepoInstallFile
from pydantic import ValidationError

STORE_APP_NAME: Final[str] = "app-store"
TIMEOUT_SECONDS: Final[float] = 30.0


def _refuse(reason: str) -> int:
    click.echo(json.dumps({"is_handed_over": False, "what_happened": reason}, indent=2))
    return 1


def what_runs(install_file: RepoInstallFile) -> tuple[object, ...]:
    """What a trial must have run for its result to stand for this file."""
    return (
        install_file.repository,
        install_file.commit,
        install_file.recipe,
        install_file.launch,
    )


def store_url() -> str | None:
    row = next(
        (
            row
            for row in read_registry(registry_path())
            if str(row.name) == STORE_APP_NAME
        ),
        None,
    )
    return None if row is None else str(row.url).replace("localhost", "127.0.0.1")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("install_file", type=Path)
    parser.add_argument(
        "--trial", type=Path, required=True, help="The trial's record directory"
    )
    parser.add_argument(
        "--replaces",
        default="",
        help="For an update: the installed app this newer version replaces",
    )
    arguments = parser.parse_args(argv)
    try:
        install_file = RepoInstallFile.model_validate(
            json.loads(arguments.install_file.read_text(encoding="utf-8"))
        )
        tried = RepoInstallFile.model_validate(
            json.loads(
                (arguments.trial / "install-file.json").read_text(encoding="utf-8")
            )
        )
        result = json.loads(
            (arguments.trial / "result.json").read_text(encoding="utf-8")
        )
    except ValidationError as e:
        return _refuse(
            f"The install file breaks a rule: {describe_validation_error(e)}"
        )
    except (OSError, ValueError) as e:
        return _refuse(f"The file or its trial couldn't be read: {e}")
    if result.get("outcome") != "WORKS":
        return _refuse(
            f"That trial didn't work ({result.get('outcome')}): {result.get('what_happened')}"
        )
    if what_runs(tried) != what_runs(install_file):
        return _refuse(
            "That trial ran something different from this file; try this one out first."
        )
    url = store_url()
    if url is None:
        return _refuse("The App Store isn't registered in this workspace.")
    chat_id = os.environ.get("MINDS_CHAT_ID") or os.environ.get("MNGR_AGENT_ID") or ""
    # The trial's picture of the app running, which the confirmation shows the person.
    screenshot = arguments.trial / "screenshot.png"
    screenshot_png_base64 = (
        base64.b64encode(screenshot.read_bytes()).decode("ascii")
        if screenshot.is_file()
        else ""
    )
    try:
        response = httpx.post(
            f"{url}/api/repo-adds",
            json={
                "install_file": install_file.model_dump(mode="json"),
                "chat_id": chat_id,
                "replaces": arguments.replaces,
                "screenshot_png_base64": screenshot_png_base64,
            },
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        return _refuse(f"The App Store couldn't be reached ({e}).")
    answer = (
        response.json()
        if response.headers.get("content-type", "").startswith("application/json")
        else {}
    )
    if response.status_code != 201:
        return _refuse(
            str(
                answer.get("detail")
                or f"The App Store answered {response.status_code}."
            )
        )
    click.echo(
        json.dumps({"is_handed_over": True, "pending": answer["pending"]}, indent=2)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
