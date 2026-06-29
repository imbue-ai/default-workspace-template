"""Editor for canonizing Minds concept terms, definitions, and value enums.

Serves a single-page editor over the concept taxonomy distilled from the
Obsidian "Taxonomizing" vault. The editable working copy lives in
``runtime/taxonomy-editor/concepts.json`` (runtime state, backed up by the
runtime-backup service); it is seeded on first run from the bundled
``assets/seed_concepts.json`` snapshot. The raw vault docs ship under
``assets/docs/`` and are rendered on demand as the immutable source the
taxonomy was built from.

The app is a plain Flask application served on a threaded werkzeug server
(mirroring ``apps/system_interface``); there is no async. The frontend issues
all requests as relative URLs, so the app serves correctly both standalone at
``/`` and when reached through the system_interface proxy at
``/service/taxonomy-editor/`` without any prefix configuration.
"""

import json
import shutil
import threading
from pathlib import Path
from typing import Any

import markdown
from flask import Flask
from flask import Response
from flask import abort
from flask import jsonify
from flask import request
from werkzeug.serving import make_server

_ASSETS_DIR = Path(__file__).parent / "assets"
_DOCS_DIR = _ASSETS_DIR / "docs"
_SEED_FILE = _ASSETS_DIR / "seed_concepts.json"
_INDEX_FILE = _ASSETS_DIR / "index.html"

# Runtime state: the editable working copy. cwd-relative so it resolves the
# same whether started by bootstrap (from /mngr/code) or standalone.
_DATA_DIR = Path("runtime/taxonomy-editor")
_DATA_FILE = _DATA_DIR / "concepts.json"

_LOCK = threading.Lock()

app = Flask(__name__)


@app.after_request
def _no_store(response: Response) -> Response:
    """The editor is a live single-user tool; never let the browser serve a
    stale build or stale data."""
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


def _load_data() -> dict[str, Any]:
    """Load the working copy, seeding it from the bundled snapshot on first run."""
    if not _DATA_FILE.exists():
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_SEED_FILE, _DATA_FILE)
    with _DATA_FILE.open(encoding="utf-8") as handle:
        return json.load(handle)


def _save_data(data: dict[str, Any]) -> None:
    """Persist the working copy atomically (write-temp-then-rename)."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _DATA_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    tmp.replace(_DATA_FILE)


def _require_object_body() -> dict[str, Any]:
    """Parse the request body as a JSON object, or reject it with a 400."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        abort(400, description="request body must be a JSON object")
    return body


@app.get("/")
def index() -> Response:
    # Version the app.js reference by its mtime so the browser refetches it
    # whenever the file changes (belt-and-suspenders with the no-store header).
    version = int((_ASSETS_DIR / "app.js").stat().st_mtime)
    html = _INDEX_FILE.read_text(encoding="utf-8").replace('src="app.js"', f'src="app.js?v={version}"')
    return Response(html, mimetype="text/html")


@app.get("/app.js")
def app_js() -> Response:
    return Response(
        (_ASSETS_DIR / "app.js").read_text(encoding="utf-8"),
        mimetype="application/javascript",
    )


@app.get("/health")
def health() -> Response:
    return jsonify({"status": "ok"})


@app.get("/api/data")
def get_data() -> Response:
    with _LOCK:
        return jsonify(_load_data())


@app.put("/api/concept/<concept_id>")
def put_concept(concept_id: str) -> Response:
    """Replace (or append) a concept by id. The frontend sends the whole record,
    so every field is editable without per-field endpoints."""
    concept = _require_object_body()
    if concept.get("id") != concept_id:
        abort(400, description="id in body must match the URL")
    with _LOCK:
        data = _load_data()
        concepts = data.setdefault("concepts", [])
        for index_position, existing in enumerate(concepts):
            if existing.get("id") == concept_id:
                concepts[index_position] = concept
                break
        else:
            concepts.append(concept)
        _save_data(data)
    return jsonify(concept)


@app.delete("/api/concept/<concept_id>")
def delete_concept(concept_id: str) -> Response:
    with _LOCK:
        data = _load_data()
        data["concepts"] = [c for c in data.get("concepts", []) if c.get("id") != concept_id]
        _save_data(data)
    return jsonify({"deleted": concept_id})


@app.put("/api/cluster/<cluster_id>")
def put_cluster(cluster_id: str) -> Response:
    cluster = _require_object_body()
    if cluster.get("id") != cluster_id:
        abort(400, description="id in body must match the URL")
    with _LOCK:
        data = _load_data()
        clusters = data.setdefault("clusters", [])
        for index_position, existing in enumerate(clusters):
            if existing.get("id") == cluster_id:
                clusters[index_position] = cluster
                break
        else:
            clusters.append(cluster)
        _save_data(data)
    return jsonify(cluster)


@app.delete("/api/cluster/<cluster_id>")
def delete_cluster(cluster_id: str) -> Response:
    with _LOCK:
        data = _load_data()
        data["clusters"] = [c for c in data.get("clusters", []) if c.get("id") != cluster_id]
        _save_data(data)
    return jsonify({"deleted": cluster_id})


@app.get("/api/export")
def export_data() -> Response:
    """Download the full working copy as a JSON file."""
    with _LOCK:
        payload = json.dumps(_load_data(), indent=2, ensure_ascii=False)
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": 'attachment; filename="concepts.json"'},
    )


def _doc_names() -> list[str]:
    names = [path.name for path in sorted(_DOCS_DIR.glob("*.md"))]
    names += [f"groups/{path.name}" for path in sorted((_DOCS_DIR / "groups").glob("*.md"))]
    return names


@app.get("/api/docs")
def list_docs() -> Response:
    return jsonify({"docs": _doc_names()})


@app.get("/api/docs/<path:doc_path>")
def get_doc(doc_path: str) -> Response:
    """Render a vault doc as HTML (the immutable source), or return raw markdown."""
    # Guard against path traversal: only allow the known doc set.
    if doc_path not in _doc_names():
        abort(404, description="unknown doc")
    text = (_DOCS_DIR / doc_path).read_text(encoding="utf-8")
    raw = request.args.get("raw", "").lower() in ("1", "true", "yes", "on")
    if raw:
        return Response(text, mimetype="text/plain")
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "toc"])
    return Response(f'<div class="markdown-body">{body}</div>', mimetype="text/html")


def main() -> None:
    server = make_server("127.0.0.1", 8081, app, threaded=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
