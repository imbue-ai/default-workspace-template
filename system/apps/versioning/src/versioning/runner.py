"""Browse an app's history as a natural-language timeline and restore any previous version.

Serves the scrub-back timeline for each app: version nodes derived from the shared
repo's history (filtered to the app's folder), plain-language summaries cached under
DATA_DIR, and a restore endpoint that records restores as new commits and revives
the app afterwards. See history.py for the tree derivation and restore.py for the
restore engine.
"""

import json
import os
import threading
import uuid
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from flask import Flask
from flask import Response
from flask import request
from flask import send_from_directory
from loguru import logger
from werkzeug.serving import run_simple

from versioning.data_types import AppHistory
from versioning.data_types import AppNotFoundError
from versioning.data_types import AppRef
from versioning.data_types import CommitRecord
from versioning.data_types import RestoreError
from versioning.data_types import SummaryGenerationError
from versioning.data_types import VersioningError
from versioning.git_repo import SubprocessGitRepo
from versioning.history import build_app_history
from versioning.history import discover_apps
from versioning.history import find_app_by_name
from versioning.history import relative_time_label
from versioning.history import short_relative_time_label
from versioning.magnitude import dot_diameter_px
from versioning.magnitude import version_phrase
from versioning.assistant import AssistError
from versioning.assistant import perform_assist
from versioning.assistant import run_assist_task
from versioning.restore import UNRESTORABLE_APP_NAMES
from versioning.restore import build_restore_preview
from versioning.restore import perform_restore
from versioning.restore import restart_service
from versioning.summaries import generate_and_cache_summary
from versioning.summaries import read_cached_summary

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/versioning/`` but is overridable via the ``VERSIONING_DATA_DIR`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill.
DATA_DIR = Path(os.environ.get("VERSIONING_DATA_DIR", "data/.apps/versioning"))

# Listen port. Defaults to this app's assigned port but is overridable via
# the ``VERSIONING_PORT`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one (see the update-app skill).
PORT = int(os.environ.get("VERSIONING_PORT", "8082"))

# The shared repo this whole workspace lives in; services run from its root.
REPO_ROOT = Path(os.environ.get("VERSIONING_REPO_ROOT", ".")).resolve()

APPS_TOML_PATH = Path(os.environ.get("VERSIONING_APPS_TOML", "data/.state/apps.toml"))

_ASSETS_DIR: Final[Path] = Path(__file__).parent / "assets"
_TIMELINE_PAGE: Final[Path] = _ASSETS_DIR / "timeline.html"

app = Flask("versioning", static_folder=None)


@app.route("/assets/<path:file_name>")
def asset(file_name: str) -> Response:
    return send_from_directory(_ASSETS_DIR, file_name)


def _git_repo() -> SubprocessGitRepo:
    return SubprocessGitRepo(repo_root=REPO_ROOT)


def _json_response(payload: object, status: int = 200) -> Response:
    return Response(json.dumps(payload), status=status, mimetype="application/json")


def _error_response(error: Exception, status: int) -> Response:
    return _json_response({"error": str(error)}, status=status)


@app.route("/")
def index() -> Response:
    # The standalone entry: show the first app's timeline, or let ?app= pick one.
    requested = request.args.get("app")
    apps = discover_apps(REPO_ROOT, APPS_TOML_PATH)
    if len(apps) == 0:
        return Response("No apps found", status=404)
    chosen = requested if requested is not None else apps[0].name
    return timeline_page(chosen)


@app.route("/app/<app_name>")
def timeline_page(app_name: str) -> Response:
    try:
        find_app_by_name(REPO_ROOT, APPS_TOML_PATH, app_name)
    except AppNotFoundError as e:
        return Response(str(e), status=404)
    page = _TIMELINE_PAGE.read_text().replace("__APP_NAME__", app_name)
    return Response(page, mimetype="text/html")


@app.route("/api/apps")
def list_apps() -> Response:
    apps = discover_apps(REPO_ROOT, APPS_TOML_PATH)
    return _json_response({"apps": [app_ref.model_dump() for app_ref in apps]})


@app.route("/api/app/<app_name>/history")
def app_history(app_name: str) -> Response:
    try:
        app_ref = find_app_by_name(REPO_ROOT, APPS_TOML_PATH, app_name)
    except AppNotFoundError as e:
        return _error_response(e, 404)
    history = build_app_history(_git_repo(), app_ref)
    now = datetime.now(timezone.utc)
    nodes_payload = []
    for node in history.nodes:
        cached_summary = read_cached_summary(DATA_DIR / "summaries", node.sha)
        nodes_payload.append(
            {
                **node.model_dump(mode="json"),
                "when_label": relative_time_label(node.authored_at, now),
                "short_when_label": short_relative_time_label(node.authored_at, now),
                "summary": cached_summary.model_dump() if cached_summary is not None else None,
                "dot_diameter_px": dot_diameter_px(node.change_stats) if node.change_stats is not None else None,
                "phrase": version_phrase(node.kind, node.change_stats),
            }
        )
    return _json_response(
        {
            "app": app_ref.model_dump(),
            "nodes": nodes_payload,
            "is_restorable": app_ref.name not in UNRESTORABLE_APP_NAMES,
        }
    )


@app.route("/api/app/<app_name>/summary/<sha>", methods=["POST"])
def node_summary(app_name: str, sha: str) -> Response:
    try:
        app_ref = find_app_by_name(REPO_ROOT, APPS_TOML_PATH, app_name)
    except AppNotFoundError as e:
        return _error_response(e, 404)
    git_repo = _git_repo()
    commits = git_repo.read_commits_touching_path(app_ref.package_dir)
    commit = next((c for c in commits if c.sha == sha), None)
    if commit is None:
        return _json_response({"error": f"No version '{sha}'"}, status=404)
    diff_excerpt = git_repo.read_diff_of_commits([sha], app_ref.package_dir)
    try:
        summary = generate_and_cache_summary(
            DATA_DIR / "summaries",
            sha,
            commit.trailers.request,
            f"{commit.subject}\n{commit.body}".strip(),
            diff_excerpt,
        )
    except (SummaryGenerationError, VersioningError) as e:
        logger.warning("Summary generation failed for {}: {}", sha, e)
        return _error_response(e, 502)
    return _json_response(summary.model_dump())


@app.route("/api/app/<app_name>/diff/<sha>")
def node_diff(app_name: str, sha: str) -> Response:
    try:
        app_ref = find_app_by_name(REPO_ROOT, APPS_TOML_PATH, app_name)
    except AppNotFoundError as e:
        return _error_response(e, 404)
    git_repo = _git_repo()
    commits = git_repo.read_commits_touching_path(app_ref.package_dir)
    commit = next((c for c in commits if c.sha == sha), None)
    if commit is None:
        return _json_response({"error": f"No version '{sha}'"}, status=404)
    diff_text = git_repo.read_diff_of_commits([sha], app_ref.package_dir)
    file_changes = git_repo.read_file_changes_of_commit(sha, app_ref.package_dir)
    return _json_response(
        {
            "sha": sha,
            "commits": [commit.model_dump(mode="json")],
            "files": [change.model_dump() for change in file_changes],
            "diff": diff_text,
        }
    )


@app.route("/api/app/<app_name>/restore", methods=["POST"])
def restore(app_name: str) -> Response:
    body = request.get_json(silent=True) or {}
    target_sha = body.get("sha")
    mode = body.get("mode", "preview")
    if not isinstance(target_sha, str) or mode not in ("preview", "apply"):
        return _json_response({"error": "Expected JSON body with sha and mode=preview|apply"}, status=400)
    try:
        app_ref = find_app_by_name(REPO_ROOT, APPS_TOML_PATH, app_name)
    except AppNotFoundError as e:
        return _error_response(e, 404)
    git_repo = _git_repo()
    history = build_app_history(git_repo, app_ref)
    try:
        preview = build_restore_preview(git_repo, history, target_sha)
    except RestoreError as e:
        return _error_response(e, 404)
    if mode == "preview":
        return _json_response(preview.model_dump())
    try:
        result = perform_restore(
            git_repo=git_repo,
            app=app_ref,
            target_sha=preview.target_sha,
            lock_file=DATA_DIR / "restore.lock",
            is_service_managed=True,
        )
    except RestoreError as e:
        logger.warning("Restore of {} failed: {}", app_name, e)
        return _error_response(e, 409)
    return _json_response(result.model_dump())


def _assist_job_file(job_id: str) -> Path:
    return DATA_DIR / "assists" / f"{job_id}.json"


def _write_assist_job(job_id: str, status: str, answer: str, new_version_sha: str | None = None) -> None:
    _assist_job_file(job_id).parent.mkdir(parents=True, exist_ok=True)
    _assist_job_file(job_id).write_text(
        json.dumps({"status": status, "answer": answer, "new_version_sha": new_version_sha})
    )


def _run_assist_job(
    job_id: str,
    app_ref: AppRef,
    sha: str,
    commit_record: CommitRecord,
    message: str,
    prior_exchanges: list[dict[str, str]],
) -> None:
    git_repo = _git_repo()
    cached_summary = read_cached_summary(DATA_DIR / "summaries", sha)
    try:
        outcome = perform_assist(
            git_repo=git_repo,
            app=app_ref,
            version_sha=sha,
            commit=commit_record,
            summary=cached_summary,
            prior_exchanges=prior_exchanges,
            message=message,
            is_change_allowed=app_ref.name not in UNRESTORABLE_APP_NAMES,
            lock_file=DATA_DIR / "restore.lock",
            task_runner=run_assist_task,
        )
    except (AssistError, VersioningError) as e:
        logger.warning("Assist failed for {}: {}", app_ref.name, e)
        _write_assist_job(job_id, "failed", str(e))
        return
    if outcome.new_version_sha is None:
        _write_assist_job(job_id, "done", outcome.answer)
        return
    # The helper changed the app: revive it so the change is live immediately.
    answer = outcome.answer
    try:
        if app_ref.program is not None:
            restart_service(app_ref.program)
    except RestoreError as e:
        answer = f"{answer}\n\nThe change is saved, but the app may need a manual restart: {e}"
    _write_assist_job(job_id, "done", answer, outcome.new_version_sha)


@app.route("/api/app/<app_name>/assist", methods=["POST"])
def start_assist(app_name: str) -> Response:
    body = request.get_json(silent=True) or {}
    sha = body.get("sha")
    message = body.get("message")
    prior_exchanges = body.get("prior", [])
    if not isinstance(sha, str) or not isinstance(message, str) or not message.strip():
        return _json_response({"error": "Expected JSON body with sha and message"}, status=400)
    try:
        app_ref = find_app_by_name(REPO_ROOT, APPS_TOML_PATH, app_name)
    except AppNotFoundError as e:
        return _error_response(e, 404)
    git_repo = _git_repo()
    commits = git_repo.read_commits_touching_path(app_ref.package_dir)
    commit_record = next((c for c in commits if c.sha == sha), None)
    if commit_record is None:
        return _json_response({"error": f"No version '{sha}'"}, status=404)
    job_id = uuid.uuid4().hex
    _write_assist_job(job_id, "running", "")
    worker = threading.Thread(
        target=_run_assist_job,
        args=(job_id, app_ref, sha, commit_record, message, prior_exchanges if isinstance(prior_exchanges, list) else []),
        daemon=True,
    )
    worker.start()
    return _json_response({"job_id": job_id})


@app.route("/api/app/<app_name>/assist/<job_id>")
def assist_status(app_name: str, job_id: str) -> Response:
    job_file = _assist_job_file(job_id)
    if not job_file.exists():
        return _json_response({"error": "No such job"}, status=404)
    return Response(job_file.read_text(), mimetype="application/json")


@app.route("/health")
def health() -> Response:
    return _json_response({"status": "ok"})


def main() -> None:
    run_simple("127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
