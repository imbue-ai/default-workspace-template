"""Atlas book viewer -- a sidebar of projects -> feature one-pagers + a page pane.

Mostly a reader: it renders the repo's atlas/ files directly (always current) and
reuses the skill's own logic -- atlas_index.gather() for the project->feature
tree, atlas_status.build_status_line() for each feature's live §0 line, and
markdown-it to render the selected page. The one write it allows is setting a
topic's lifecycle status (POST /topic/<slug>/status), which rewrites just the
`status` line in that topic's declaration.
"""

import json
import os
import re
import subprocess
import sys
import time
from html import escape
from pathlib import Path

from flask import Flask, Response, abort, request
from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin
from werkzeug.serving import run_simple

PORT = int(os.environ.get("ATLAS_BOOK_PORT", "8082"))


def _repo_root() -> Path:
    override = os.environ.get("ATLAS_BOOK_REPO_ROOT")
    if override:
        return Path(override).resolve()
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    return Path(top.stdout.strip()) if top.returncode == 0 else Path.cwd()


REPO_ROOT = _repo_root()
# Reuse the atlas skill's own logic rather than reimplementing it.
sys.path.insert(0, str(REPO_ROOT / ".agents" / "skills" / "atlas" / "scripts"))
import atlas_index  # noqa: E402
import atlas_status  # noqa: E402
import atlas_topic  # noqa: E402

# footnote_plugin renders `[^id]` citation markers as superscript links to the
# Evidence, instead of printing the raw `[^id]` text.
_MD = (
    MarkdownIt("commonmark", {"html": True, "linkify": True})
    .enable("table")
    .use(footnote_plugin)
)
app = Flask("atlas_book", static_folder=None)

# The lifecycle states, owned by the skill (single source of truth); picker order.
STATUS_ORDER = atlas_topic.STATUSES


def _state_class(status_line: str) -> str:
    first = status_line.split(" ", 1)[0].strip("*").upper()
    return {
        "RUNNING": "running",
        "WAITING": "waiting",
        "PROPOSED": "proposed",
        "SHIPPED": "dormant",
        "PAUSED": "waiting",
        "ABANDONED": "dormant",
    }.get(first, "dormant")


def _known_slugs() -> set[str]:
    return {
        f["slug"] for feats in atlas_index.gather(REPO_ROOT).values() for f in feats
    }


def _status_for(slug: str) -> str:
    try:
        return atlas_status.build_status_line(REPO_ROOT, slug, time.time())
    except atlas_status.DeclarationError:
        return "unknown"


def _declared_status(slug: str) -> str:
    """The topic's human-set lifecycle status from its declaration (not the §0 line)."""
    try:
        return str(
            atlas_status.load_declaration(REPO_ROOT, slug).get("status") or ""
        ).lower()
    except atlas_status.DeclarationError:
        return ""


def _controls_html(slug: str) -> str:
    """A dark, app-matching status picker (custom dropdown, not a native select).

    Slug/status travel in data-* attributes and are handled by delegated listeners
    (see the PAGE script) -- no slug is interpolated into inline JS, so a slug with
    a quote can neither break the handler nor inject script.
    """
    declared = _declared_status(slug)
    current = declared or "—"
    opts = "".join(
        f'<button class="statusopt{" sel" if s == declared else ""}" '
        f'data-slug="{escape(slug)}" data-status="{s}">{s}</button>'
        for s in STATUS_ORDER
    )
    return (
        '<div class="statuspick">'
        f'<button class="statustrigger" data-slug="{escape(slug)}">'
        '<span class="k">Status</span>'
        f'<span class="v">{escape(current)}</span>'
        '<span class="caret"></span>'
        "</button>"
        f'<div class="statusmenu">{opts}</div>'
        "</div>"
    )


def render_sidebar() -> str:
    projects = atlas_index.gather(REPO_ROOT)
    out = ['<div class="brand">Atlas <small>· project book</small></div>']
    for project in sorted(projects):
        title = atlas_index.project_title(REPO_ROOT, project)
        out.append(f'<div class="project">{escape(title)}</div>')
        for feat in sorted(projects[project], key=lambda f: f["mtime"], reverse=True):
            status = _status_for(feat["slug"])
            lifecycle = str(feat.get("status") or "").lower() or "unknown"
            out.append(
                f'<a class="feat" data-slug="{escape(feat["slug"])}" '
                f'onclick="load(this)">'
                f'<div class="row"><span class="dot {_state_class(status)}"></span>'
                f'<span class="title">{escape(feat["title"])}</span>'
                f'<span class="badge b-{escape(lifecycle)}">{escape(lifecycle)}</span></div>'
                f'<div class="status">{escape(status)}</div></a>'
            )
    return "\n".join(out)


def _strip_status_block(md: str) -> str:
    """Drop the raw §0 marker block; the live status is shown separately."""
    md = re.sub(
        r"<!-- atlas:status -->.*?<!-- /atlas:status -->\n?",
        "",
        md,
        flags=re.DOTALL,
    )
    md = re.sub(r"\*\(§0 above is machine-generated[^\n]*\)\*\n?", "", md)
    return md


def _strip_unconfirmed_banner(md: str) -> str:
    """Drop any 'Unconfirmed' blockquote -- shown only while a topic is proposed.

    Once a human confirms the topic, the picker reflects it immediately, but the
    banner is baked into the page markdown until the next full generation; strip
    it here so a confirmed page doesn't keep contradicting its own status.
    """
    return re.sub(
        r"(?m)^(?:[ \t]*>[^\n]*\n?)+\n?",
        lambda m: "" if "Unconfirmed" in m.group(0) else m.group(0),
        md,
    )


def _section(md: str, header: str) -> str:
    """Body between `## header` and the next section boundary (heading/sources)."""
    lines = md.split("\n")
    start = next((i for i, ln in enumerate(lines) if ln.strip() == header), None)
    if start is None:
        return ""
    end = next(
        (
            j
            for j in range(start + 1, len(lines))
            if lines[j].startswith("## ")
            or lines[j].startswith("<details>")
            or lines[j].startswith("[^")
        ),
        len(lines),
    )
    return "\n".join(lines[start + 1 : end]).strip()


def _strip_citations(text: str) -> str:
    """Remove citation markers and the live-refresh note for the plain view."""
    text = re.sub(r"\[\^[A-Za-z0-9_-]+\](?!:)", "", text)  # [^id] refs
    text = re.sub(r"\*\(auto-refreshed[^)]*\)\*", "", text)  # live-refresh note
    return re.sub(r"[ \t]{2,}", " ", text).strip()


# Placeholder bodies that should render as "nothing here yet", not real content.
_EMPTY_BODIES = {"(none)", "(pending.)", "(this page fills in as the work continues.)"}


def _render_overview(md: str) -> str:
    """A high-level, non-technical view derived from the page's own sections."""
    parts = []
    title_match = re.search(r"^# (.+)$", md, re.MULTILINE)
    if title_match:
        parts.append(f"<h1>{escape(title_match.group(1).strip())}</h1>")

    def add(title: str, section_header: str) -> None:
        body = _strip_citations(_section(md, section_header))
        if body and body.strip().lower() not in _EMPTY_BODIES:
            parts.append(f"<h2>{escape(title)}</h2>\n{_MD.render(body)}")

    add("What it does", "## Why this exists")
    add("What it supports today", "## Current state")
    add("What's next", "## Next steps")
    add("Open questions", "## Open questions")
    if not parts:
        parts.append(
            '<p class="empty">No overview yet — this page has not been written up.</p>'
        )
    return "".join(parts)


@app.route("/page/<slug>")
def page(slug: str) -> Response:
    if slug not in _known_slugs():
        abort(404)
    md_path = REPO_ROOT / "atlas" / f"{slug}.md"
    if not md_path.is_file():
        abort(404)
    mode = (request.args.get("mode") or "overview").lower()
    body_md = _strip_status_block(md_path.read_text(encoding="utf-8"))
    if _declared_status(slug) not in ("", "proposed"):
        body_md = _strip_unconfirmed_banner(body_md)
    if mode == "technical":
        chip = (
            f"{_controls_html(slug)}"
            f'<div class="status0">{escape(_status_for(slug))}</div>'
            f'<article class="page">{_MD.render(body_md)}</article>'
        )
    else:
        chip = (
            f"{_controls_html(slug)}"
            f'<article class="page overview">{_render_overview(body_md)}</article>'
        )
    return Response(chip, mimetype="text/html")


@app.route("/topic/<slug>/status", methods=["POST"])
def set_status(slug: str) -> Response:
    if slug not in _known_slugs():
        abort(404)
    status = (request.form.get("status") or "").strip().lower()
    try:
        atlas_topic.set_status(REPO_ROOT, slug, status)
    except atlas_topic.UnknownStatus:
        abort(400)
    except atlas_topic.TopicNotFound:
        abort(404)
    except atlas_topic.StatusWriteFailed:
        abort(500)
    return Response(
        json.dumps({"slug": slug, "status": status}), mimetype="application/json"
    )


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Atlas</title>
<style>
  :root { color-scheme: dark; --bg:#0b1120; --panel:#0f172a; --line:rgba(148,163,184,.16);
          --muted:#94a3b8; --dim:#64748b; --fg:#e2e8f0; --accent:#38bdf8; }
  * { box-sizing: border-box; }
  html,body { margin:0; height:100%; }
  body { display:flex; height:100vh; font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         background:var(--bg); color:var(--fg); }
  aside { width:310px; flex:none; border-right:1px solid var(--line); background:var(--panel);
          overflow-y:auto; padding:1rem .75rem; }
  .brand { font-weight:700; letter-spacing:-.02em; font-size:1.1rem; padding:.25rem .5rem 1rem; }
  .brand small { color:var(--dim); font-weight:500; }
  .project { margin:.6rem 0 .25rem; padding:.35rem .5rem; font-size:.72rem; font-weight:700;
             text-transform:uppercase; letter-spacing:.08em; color:var(--dim); }
  a.feat { display:block; text-decoration:none; color:var(--fg); padding:.5rem .6rem; border-radius:9px;
           margin-bottom:.15rem; cursor:pointer; }
  a.feat:hover { background:rgba(148,163,184,.08); }
  a.feat.active { background:rgba(56,189,248,.14); border:1px solid rgba(56,189,248,.35); }
  .feat .row { display:flex; align-items:center; gap:.5rem; }
  .dot { width:8px; height:8px; border-radius:50%; flex:none; }
  .dot.running { background:#4ade80; } .dot.waiting { background:#fbbf24; }
  .dot.dormant { background:#64748b; } .dot.proposed { background:#38bdf8; }
  .feat .title { font-size:.9rem; font-weight:600; }
  .feat .badge { margin-left:auto; flex:none; font-size:.6rem; font-weight:700; text-transform:uppercase;
                 letter-spacing:.05em; padding:.12rem .4rem; border-radius:999px; border:1px solid transparent; }
  .badge.b-proposed  { color:#7dd3fc; background:rgba(56,189,248,.12); border-color:rgba(56,189,248,.3); }
  .badge.b-active    { color:#4ade80; background:rgba(74,222,128,.12); border-color:rgba(74,222,128,.3); }
  .badge.b-paused    { color:#fbbf24; background:rgba(251,191,36,.12); border-color:rgba(251,191,36,.3); }
  .badge.b-shipped   { color:#a5b4fc; background:rgba(129,140,248,.12); border-color:rgba(129,140,248,.3); }
  .badge.b-abandoned { color:#94a3b8; background:rgba(148,163,184,.1); border-color:rgba(148,163,184,.25); }
  .badge.b-unknown   { color:var(--dim); background:rgba(148,163,184,.08); }
  .feat .status { font-size:.72rem; color:var(--dim); margin-top:.15rem; padding-left:1rem;
                  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  main { flex:1; overflow-y:auto; padding:2.5rem 3rem; }
  .wrap { max-width:46rem; margin:0 auto; }
  .statuspick { position:relative; display:inline-block; margin-bottom:1rem; }
  .statustrigger { display:inline-flex; align-items:center; gap:.5rem; background:rgba(148,163,184,.06);
                   color:var(--fg); border:1px solid var(--line); border-radius:8px; padding:.4rem .7rem;
                   font-size:.82rem; cursor:pointer; font-family:inherit; }
  .statustrigger:hover { border-color:rgba(56,189,248,.35); }
  .statustrigger .k { color:var(--dim); text-transform:uppercase; letter-spacing:.07em;
                      font-size:.66rem; font-weight:700; }
  .statustrigger .v { font-weight:600; }
  .statustrigger .caret { width:0; height:0; border-left:4px solid transparent; border-right:4px solid transparent;
                          border-top:5px solid var(--muted); margin-left:.15rem; transition:transform .12s; }
  .statuspick.open .statustrigger .caret { transform:rotate(180deg); }
  .statusmenu { position:absolute; top:calc(100% + 5px); left:0; z-index:30; min-width:170px; display:none;
                background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:.3rem;
                box-shadow:0 12px 34px rgba(0,0,0,.5); }
  .statuspick.open .statusmenu { display:block; }
  .statusopt { display:block; width:100%; text-align:left; background:none; border:none; color:var(--fg);
               padding:.42rem .55rem; border-radius:7px; font-size:.82rem; cursor:pointer; font-family:inherit; }
  .statusopt:hover { background:rgba(56,189,248,.14); }
  .statusopt.sel { color:var(--accent); font-weight:700; }
  .status0 { font-family: ui-monospace, monospace; font-size:.8rem; color:var(--muted);
             background:rgba(148,163,184,.06); border:1px solid var(--line); border-radius:8px;
             padding:.5rem .8rem; margin-bottom:1.5rem; }
  .page h1 { letter-spacing:-.02em; margin:.2rem 0 1rem; }
  .page h2 { font-size:1.05rem; margin:1.6rem 0 .5rem; padding-bottom:.3rem; border-bottom:1px solid var(--line); }
  .page p, .page li { line-height:1.6; color:#cbd5e1; }
  .page blockquote { border-left:3px solid rgba(251,191,36,.5); background:rgba(251,191,36,.08);
                     margin:0 0 1.25rem; padding:.6rem .9rem; color:#fde68a; border-radius:0 8px 8px 0; }
  .page code { background:rgba(148,163,184,.12); padding:.1rem .35rem; border-radius:5px; font-size:.85em; }
  .page table { border-collapse:collapse; width:100%; font-size:.82rem; margin:.5rem 0; }
  .page th, .page td { border:1px solid var(--line); padding:.35rem .5rem; text-align:left; vertical-align:top; }
  .page details { margin-top:1.5rem; } .page summary { cursor:pointer; color:var(--muted); }
  .empty { color:var(--dim); text-align:center; margin-top:20vh; }
  .modebar { display:flex; gap:.25rem; margin-bottom:1.25rem; background:rgba(148,163,184,.06);
             border:1px solid var(--line); border-radius:9px; padding:.25rem; width:fit-content; }
  .modebtn { background:none; border:none; color:var(--muted); font-family:inherit; font-size:.8rem;
             font-weight:600; padding:.35rem .85rem; border-radius:7px; cursor:pointer; }
  .modebtn:hover { color:var(--fg); }
  .modebtn.active { background:var(--accent); color:#04222e; }
  .page.overview h2 { font-size:.72rem; text-transform:uppercase; letter-spacing:.08em; color:var(--dim);
                      border-bottom:none; margin:1.5rem 0 .35rem; padding:0; }
  .page.overview h1 { margin-bottom:1rem; }
  .page.overview p, .page.overview li { color:#e2e8f0; }
</style>
</head>
<body>
  <aside>__SIDEBAR__</aside>
  <main><div class="wrap">
    <div class="modebar" id="modebar" style="display:none">
      <button class="modebtn active" data-mode="overview" onclick="setMode('overview')">Overview</button>
      <button class="modebtn" data-mode="technical" onclick="setMode('technical')">Technical</button>
    </div>
    <div id="content"><div class="empty">Select a one-pager from the left.</div></div>
  </div></main>
<script>
  let currentSlug = null;
  let currentMode = "overview";  // friendly view by default
  function markActive(slug) {
    document.querySelectorAll("a.feat").forEach(a => a.classList.toggle("active", a.dataset.slug === slug));
  }
  function setMode(m) {
    currentMode = m;
    document.querySelectorAll(".modebtn").forEach(b => b.classList.toggle("active", b.dataset.mode === m));
    if (currentSlug) show(currentSlug);
  }
  async function show(slug) {
    const url = "/page/" + encodeURIComponent(slug) + "?mode=" + currentMode;
    document.getElementById("content").innerHTML = await (await fetch(url)).text();
    document.getElementById("modebar").style.display = "flex";
  }
  async function load(el) {
    currentSlug = el.dataset.slug;
    markActive(currentSlug);
    await show(currentSlug);
    document.querySelector("main").scrollTop = 0;
    history.replaceState(null, "", "#" + currentSlug);
  }
  async function refreshSidebar() {
    document.querySelector("aside").innerHTML = await (await fetch("/sidebar")).text();
    if (currentSlug) markActive(currentSlug);
  }
  function closeMenus() {
    document.querySelectorAll(".statuspick.open").forEach(e => e.classList.remove("open"));
  }
  // Delegated so it keeps working after the sidebar/page fragments are re-injected.
  document.addEventListener("click", (ev) => {
    const trigger = ev.target.closest(".statustrigger");
    const opt = ev.target.closest(".statusopt");
    if (trigger) {
      ev.stopPropagation();
      const pick = trigger.closest(".statuspick");
      const wasOpen = pick.classList.contains("open");
      closeMenus();
      if (!wasOpen) pick.classList.add("open");
    } else if (opt) {
      ev.stopPropagation();
      closeMenus();
      setStatus(opt.dataset.slug, opt.dataset.status);
    } else {
      closeMenus();
    }
  });
  async function setStatus(slug, status) {
    try {
      const res = await fetch("/topic/" + encodeURIComponent(slug) + "/status", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: "status=" + encodeURIComponent(status),
      });
      if (!res.ok) { alert("Couldn't update status (" + res.status + ")."); return; }
    } catch (e) { alert("Couldn't update status."); return; }
    await refreshSidebar();
    if (currentSlug === slug) await show(slug);
  }
  // Open the hash target, else the first feature.
  const want = location.hash.slice(1);
  const first = [...document.querySelectorAll("a.feat")].find(a => !want || a.dataset.slug === want)
             || document.querySelector("a.feat");
  if (first) load(first);
  // Live: every 30s refresh the sidebar statuses and the open page -- but not
  // while a status menu is open, so the poll never yanks it out from under the user.
  setInterval(async () => {
    if (document.querySelector(".statuspick.open")) return;
    try { await refreshSidebar(); if (currentSlug) await show(currentSlug); } catch (e) {}
  }, 30000);
</script>
</body>
</html>"""


@app.route("/sidebar")
def sidebar() -> Response:
    return Response(render_sidebar(), mimetype="text/html")


@app.route("/")
def index() -> Response:
    return Response(PAGE.replace("__SIDEBAR__", render_sidebar()), mimetype="text/html")


@app.route("/health")
def health() -> Response:
    return Response('{"status": "ok"}', mimetype="application/json")


def main() -> None:
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
