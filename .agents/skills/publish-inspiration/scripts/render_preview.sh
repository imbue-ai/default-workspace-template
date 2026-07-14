#!/usr/bin/env bash
# Render a static, self-contained PREVIEW page for an assembled inspiration
# snapshot, used by the publish-inspiration skill's chat confirmation (its
# section 6). The LEAD runs this (never the assembly worker), after the worker
# reports done, pointing it at the worker's worktree ($WT). The output is a
# single index.html plus a copy of the thumbnail SVG; the lead serves the
# output dir with a localhost `python3 -m http.server` and registers it as the
# "inspiration-preview" tab via scripts/forward_port.py.
#
# Every fact on the page is DERIVED from the assembled snapshot itself, never
# invented: title/description/format from inspiration-<slug>.md's
# front-matter, the manifest body, the thumbnail SVG, and the file tree from
# `git ls-tree HEAD`. The page carries:
#   - a prominent PREVIEW banner pinned at the top (nothing has been published
#     yet; publishing happens only after the user confirms in chat), repeated
#     near the bottom;
#   - a "What is in this preview" note (rendered locally from the snapshot;
#     the thumbnail is mock data only; nothing has left the machine);
#   - a "What WILL be published" breakdown (the selected/overlaid paths, the
#     generated manifest/thumbnail/README/welcome, carried-forward earlier
#     inspirations, the template base's top-level tree, and the guarantee that
#     the published git history is ONLY the public template history plus one
#     snapshot commit);
#   - a "What is NOT published" breakdown (chats/transcripts/memory, secrets
#     and .env files blocked by the two-scanner secret gate, unselected
#     apps/files, the mind's git history, uploads, plus any published-version
#     modifications passed via --modification);
#   - the manifest body rendered with a minimal dependency-free markdown
#     converter (python3 stdlib only).
# It is fully self-contained: one inline <style>, no external resources.
#
# Usage:
#   render_preview.sh --worktree <path-to-$WT> --slug <slug> --out <dir>
#                     [--include <path> ...] [--data-include <path> ...]
#                     [--modification <text> ...]
#
# --include / --data-include are the same repo-root-relative paths the lead
# passed to build_inspiration.sh; pass them explicitly (preferred, fully
# deterministic). If omitted, the overlaid paths are derived from the snapshot
# itself: the name-diff between the `inspiration: <slug>` assembly commit's
# parent (the clean template base) and HEAD, collapsed to two path components.
#
# Exit codes: 0 = preview written to <dir>/index.html; 1 = missing inputs
# (worktree/manifest/thumbnail not found, or include paths underivable);
# 2 = usage error.

set -euo pipefail

# --- argument parsing --------------------------------------------------------

WORKTREE=""
SLUG=""
OUT_DIR=""
INCLUDE_PATHS=()
DATA_INCLUDE_PATHS=()
MODIFICATIONS=()

usage() {
    cat >&2 <<'USAGE'
Usage: render_preview.sh --worktree <path> --slug <slug> --out <dir>
                         [--include <path> ...] [--data-include <path> ...]
                         [--modification <text> ...]
USAGE
    exit 2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --worktree)
            WORKTREE="${2:-}"
            shift 2
            ;;
        --slug)
            SLUG="${2:-}"
            shift 2
            ;;
        --out)
            OUT_DIR="${2:-}"
            shift 2
            ;;
        --include)
            INCLUDE_PATHS+=("${2:-}")
            shift 2
            ;;
        --data-include)
            DATA_INCLUDE_PATHS+=("${2:-}")
            shift 2
            ;;
        --modification)
            MODIFICATIONS+=("${2:-}")
            shift 2
            ;;
        -h | --help)
            usage
            ;;
        *)
            echo "render_preview.sh: unknown argument: $1" >&2
            usage
            ;;
    esac
done

if [ -z "$WORKTREE" ] || [ -z "$SLUG" ] || [ -z "$OUT_DIR" ]; then
    echo "render_preview.sh: --worktree, --slug, and --out are required" >&2
    usage
fi
if ! printf '%s' "$SLUG" | grep -Eq '^[A-Za-z0-9._-]+$' || case "$SLUG" in -*) true ;; *) false ;; esac; then
    echo "render_preview.sh: slug must match ^[A-Za-z0-9._-]+\$ and not start with '-': $SLUG" >&2
    exit 2
fi

MANIFEST="inspiration-${SLUG}.md"
THUMBNAIL="inspiration-${SLUG}.svg"

# --- validate the snapshot inputs --------------------------------------------

if ! git -C "$WORKTREE" rev-parse --show-toplevel > /dev/null 2>&1; then
    echo "render_preview.sh: --worktree is not a git worktree: $WORKTREE" >&2
    exit 1
fi
for required in "$MANIFEST" "$THUMBNAIL"; do
    if [ ! -f "$WORKTREE/$required" ]; then
        echo "render_preview.sh: $required not found at the worktree root -- is the snapshot assembled and the slug right?" >&2
        exit 1
    fi
done

mkdir -p "$OUT_DIR"
TMP="$(mktemp -d)"
cleanup() {
    rm -rf "$TMP"
}
trap cleanup EXIT

# --- gather facts from the assembled snapshot --------------------------------

# Top-level entries of the tree that will be published (HEAD is the worker's
# final, clean state -- the lead verified `git status` is clean before this).
git -C "$WORKTREE" ls-tree --name-only HEAD > "$TMP/tree.txt"

# Overlaid paths: prefer the explicit flags; otherwise derive them from the
# snapshot's own history (the assembly commit `inspiration: <slug>` is
# parented directly on the clean template base, so parent..HEAD names exactly
# the overlaid + generated content).
: > "$TMP/includes.txt"
: > "$TMP/data_includes.txt"
if [ "${#INCLUDE_PATHS[@]}" -gt 0 ]; then
    for rel in "${INCLUDE_PATHS[@]}"; do
        printf '%s\n' "$rel" >> "$TMP/includes.txt"
    done
else
    assembly_commit="$(git -C "$WORKTREE" log --first-parent --format='%H %s' HEAD \
        | awk -v want="inspiration: ${SLUG}" '{h = $1; sub(/^[^ ]+ /, ""); if ($0 == want) { print h; exit } }')"
    if [ -z "$assembly_commit" ]; then
        echo "render_preview.sh: no --include flags given and no 'inspiration: ${SLUG}' commit found to derive them from -- pass the include paths explicitly" >&2
        exit 1
    fi
    # Collapse the per-file diff to (at most) two path components, dropping the
    # files the assembly generated (they get their own section on the page).
    git -C "$WORKTREE" diff --name-only "${assembly_commit}^" HEAD \
        | awk -F/ '
            $0 == "README.md" { next }
            $0 == ".agents/skills/welcome/SKILL.md" { next }
            NF == 1 && /^inspiration-/ { next }
            NF == 1 { print; next }
            { print $1 "/" $2 }
        ' | sort -u > "$TMP/includes.txt"
fi
if [ "${#DATA_INCLUDE_PATHS[@]}" -gt 0 ]; then
    for rel in "${DATA_INCLUDE_PATHS[@]}"; do
        printf '%s\n' "$rel" >> "$TMP/data_includes.txt"
    done
fi

# Published-version modifications (user-confirmed edits applied only to the
# published snapshot). Empty file -> the page says "none requested".
: > "$TMP/modifications.txt"
if [ "${#MODIFICATIONS[@]}" -gt 0 ]; then
    for mod in "${MODIFICATIONS[@]}"; do
        printf '%s\n' "$mod" >> "$TMP/modifications.txt"
    done
fi

# Generated files actually present in the tree (manifest, thumbnail, README,
# the inspiration-specific welcome).
: > "$TMP/generated.txt"
for gf in "$MANIFEST" "$THUMBNAIL" "README.md" ".agents/skills/welcome/SKILL.md"; do
    if [ -n "$(git -C "$WORKTREE" ls-tree --name-only HEAD -- "$gf")" ]; then
        printf '%s\n' "$gf" >> "$TMP/generated.txt"
    fi
done

# Earlier inspirations carried forward at the repo root (accumulation), with
# their titles from each manifest's front-matter.
: > "$TMP/carried.txt"
while IFS= read -r entry; do
    case "$entry" in
        "inspiration-${SLUG}.md") ;;
        inspiration-*.md)
            c_title="$(sed -n 's/^title: //p' "$WORKTREE/$entry" | head -1)"
            if [ -z "$c_title" ]; then
                c_title="${entry#inspiration-}"
                c_title="${c_title%.md}"
            fi
            printf '%s\t%s\n' "$entry" "$c_title" >> "$TMP/carried.txt"
            ;;
    esac
done < "$TMP/tree.txt"

# The thumbnail is copied next to index.html and referenced relatively, so the
# page stays self-contained wherever the directory is served from.
cp "$WORKTREE/$THUMBNAIL" "$OUT_DIR/$THUMBNAIL"

# --- render index.html (python3 stdlib only) ----------------------------------

PV_SLUG="$SLUG" \
    PV_MANIFEST_PATH="$WORKTREE/$MANIFEST" \
    PV_THUMBNAIL_NAME="$THUMBNAIL" \
    PV_OUT_DIR="$OUT_DIR" \
    PV_TMP_DIR="$TMP" \
    python3 - <<'PYEOF'
import html
import os
import re
from pathlib import Path

slug = os.environ["PV_SLUG"]
manifest_path = Path(os.environ["PV_MANIFEST_PATH"])
thumbnail_name = os.environ["PV_THUMBNAIL_NAME"]
out_dir = Path(os.environ["PV_OUT_DIR"])
tmp_dir = Path(os.environ["PV_TMP_DIR"])


def read_lines(name: str) -> list[str]:
    text = (tmp_dir / name).read_text(encoding="utf-8")
    return [line for line in text.split("\n") if line.strip()]


includes = read_lines("includes.txt")
data_includes = read_lines("data_includes.txt")
modifications = read_lines("modifications.txt")
generated = read_lines("generated.txt")
tree_entries = read_lines("tree.txt")
carried = [line.split("\t", 1) for line in read_lines("carried.txt")]

# --- manifest front-matter + body ---
manifest_text = manifest_path.read_text(encoding="utf-8")
meta: dict[str, str] = {}
body = manifest_text
lines = manifest_text.split("\n")
if lines and lines[0].strip() == "---":
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            for raw in lines[1:i]:
                if ":" in raw:
                    key, value = raw.split(":", 1)
                    meta[key.strip()] = value.strip()
            body = "\n".join(lines[i + 1 :])
            break

title = meta.get("title", slug)
description = meta.get("description", "")
fmt = meta.get("format", "v1 (implied; manifest predates the format key)")


# --- minimal, dependency-free markdown -> HTML for the manifest body ---
def inline(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)


def render_markdown(md: str) -> str:
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []
    code_lines: list[str] = []
    in_code = False

    def flush_para() -> None:
        if para:
            out.append("<p>%s</p>" % inline(" ".join(para)))
            para.clear()

    def flush_list() -> None:
        if items:
            lis = "".join("<li>%s</li>" % inline(item) for item in items)
            out.append("<ul>%s</ul>" % lis)
            items.clear()

    for raw in md.split("\n"):
        if in_code:
            if raw.strip().startswith("```"):
                out.append("<pre>%s</pre>" % html.escape("\n".join(code_lines)))
                code_lines.clear()
                in_code = False
            else:
                code_lines.append(raw)
            continue
        stripped = raw.strip()
        if stripped.startswith("```"):
            flush_para()
            flush_list()
            in_code = True
            continue
        if not stripped:
            flush_para()
            flush_list()
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            flush_para()
            flush_list()
            # Shift down so manifest headings sit under the page's own h1/h2.
            level = min(len(heading.group(1)) + 2, 6)
            out.append("<h%d>%s</h%d>" % (level, inline(heading.group(2)), level))
            continue
        if stripped.startswith(("- ", "* ")):
            flush_para()
            items.append(stripped[2:])
            continue
        if items and raw[:1] in (" ", "\t"):
            # Continuation line of a wrapped bullet.
            items[-1] += " " + stripped
            continue
        flush_list()
        para.append(stripped)
    if in_code:
        out.append("<pre>%s</pre>" % html.escape("\n".join(code_lines)))
    flush_para()
    flush_list()
    return "\n".join(out)


# --- "What WILL be published" pieces ---
def li_code(path: str, note: str = "") -> str:
    suffix = " <span class=\"tag\">%s</span>" % html.escape(note) if note else ""
    return "<li><code>%s</code>%s</li>" % (html.escape(path), suffix)


selected_lis = [li_code(p) for p in includes]
selected_lis += [li_code(p, "data — explicitly opted in") for p in data_includes]

generated_notes = {
    "inspiration-%s.md" % slug: "the manifest (this page's source of truth)",
    "inspiration-%s.svg" % slug: "the thumbnail (mock data only)",
    "README.md": "the repo landing page, describing this inspiration",
    ".agents/skills/welcome/SKILL.md": "the welcome for minds created from this repo",
}
generated_lis = [li_code(g, generated_notes.get(g, "generated")) for g in generated]

carried_lis = [
    "<li><strong>%s</strong> — <code>%s</code></li>" % (html.escape(c_title), html.escape(fname))
    for fname, c_title in carried
]

all_selected = includes + data_includes
data_set = set(data_includes)
tree_lis = []
for entry in tree_entries:
    contained = [p for p in all_selected if p.startswith(entry + "/")]
    if entry in generated:
        note = "generated for this inspiration"
    elif entry.startswith("inspiration-") and "/" not in entry and entry not in generated:
        note = "earlier inspiration, carried forward"
    elif entry in all_selected:
        note = "your selected path" + (" (data)" if entry in data_set else "")
    elif contained:
        note = "template base — also holds your selected %s" % ", ".join(
            "<code>%s</code>" % html.escape(p) for p in contained
        )
        tree_lis.append("<li><code>%s</code> <span class=\"tag\">%s</span></li>" % (html.escape(entry), note))
        continue
    else:
        note = "template base"
    tree_lis.append(li_code(entry, note))

if modifications:
    mods_html = (
        "<p>Published-version modifications applied to the snapshot"
        " (your live files are untouched):</p><ul>%s</ul>"
        % "".join("<li>%s</li>" % inline(m) for m in modifications)
    )
else:
    mods_html = "<p>No published-version modifications were requested.</p>"

carried_html = (
    "<h3>Carried forward: inspirations published earlier from this mind</h3><ul>%s</ul>"
    % "".join(carried_lis)
    if carried_lis
    else ""
)

page = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Preview: {title}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: #f3f4f6;
    color: #1f2933;
    line-height: 1.55;
  }}
  .preview-banner {{
    position: sticky;
    top: 0;
    z-index: 10;
    width: 100%;
    background: #b45309;
    color: #ffffff;
    text-align: center;
    padding: 14px 16px;
    border-bottom: 4px solid #7c2d12;
  }}
  .preview-banner .headline {{
    font-size: 1.15rem;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }}
  .preview-banner .subline {{
    font-size: 0.9rem;
    opacity: 0.95;
    margin-top: 2px;
  }}
  main {{ max-width: 880px; margin: 0 auto; padding: 24px 20px 48px; }}
  section {{
    background: #ffffff;
    border: 1px solid #d9dde3;
    border-radius: 10px;
    padding: 20px 24px;
    margin: 20px 0;
  }}
  h1 {{ font-size: 1.6rem; margin: 18px 0 4px; }}
  h2 {{ font-size: 1.2rem; margin: 0 0 12px; border-bottom: 1px solid #e4e7eb; padding-bottom: 8px; }}
  h3 {{ font-size: 1.02rem; margin: 18px 0 6px; }}
  h4, h5, h6 {{ font-size: 0.95rem; margin: 14px 0 4px; }}
  p {{ margin: 8px 0; }}
  ul {{ margin: 8px 0; padding-left: 24px; }}
  li {{ margin: 4px 0; }}
  code {{
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.88em;
    background: #eef1f4;
    border-radius: 4px;
    padding: 1px 5px;
  }}
  pre {{
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.85em;
    background: #1f2933;
    color: #e4e7eb;
    border-radius: 8px;
    padding: 14px;
    overflow-x: auto;
  }}
  .tag {{
    font-size: 0.78rem;
    color: #52606d;
    background: #eef1f4;
    border-radius: 999px;
    padding: 1px 8px;
    margin-left: 6px;
  }}
  .meta {{ color: #52606d; font-size: 0.92rem; }}
  .thumb {{
    display: block;
    width: 300px;
    max-width: 100%;
    border: 1px solid #d9dde3;
    border-radius: 8px;
    margin: 14px 0;
  }}
  .history-note {{
    background: #ecfdf5;
    border: 1px solid #a7f3d0;
    border-radius: 8px;
    padding: 10px 14px;
    margin-top: 14px;
  }}
  .not-published li {{ margin: 6px 0; }}
  footer {{
    text-align: center;
    color: #7c2d12;
    font-weight: 700;
    padding: 8px 16px 32px;
  }}
</style>
</head>
<body>
<div class="preview-banner" role="status">
  <div class="headline">Preview — nothing has been published yet</div>
  <div class="subline">This page is a local preview. Publishing happens only after you confirm in chat.</div>
</div>
<main>
<h1>{title}</h1>
<p>{description}</p>
<p class="meta">Inspiration slug: <code>{slug}</code> · manifest format: <code>{fmt}</code></p>
<img class="thumb" src="{thumbnail}" alt="Thumbnail for {title}">

<section>
<h2>What is in this preview</h2>
<ul>
<li>This page is rendered locally from the assembled snapshot itself — the exact files that would be published.</li>
<li>The thumbnail above uses mock data only, never your real data.</li>
<li>Nothing on this page, and nothing in the snapshot, has left this machine. No repository exists yet.</li>
</ul>
</section>

<section>
<h2>What WILL be published</h2>
<h3>Your selected app/feature paths (copied from your mind)</h3>
<ul>{selected_lis}</ul>
<h3>Generated for this inspiration</h3>
<ul>{generated_lis}</ul>
{carried_html}
<h3>Full top-level contents of the published repo</h3>
<ul>{tree_lis}</ul>
<p class="history-note"><strong>Git history:</strong> the published repo's history is ONLY the public
template's history plus <strong>one</strong> snapshot commit containing the tree above. None of your
mind's own commits are included.</p>
</section>

<section class="not-published">
<h2>What is NOT published</h2>
<ul>
<li><strong>Your chats, transcripts, and memory</strong> — <code>runtime/</code> is never part of the snapshot.</li>
<li><strong>Your secrets and <code>.env</code> files</strong> — assembly is blocked by a two-scanner secret gate (betterleaks + kingfisher); any finding aborts the publish before anything is committed.</li>
<li><strong>Your mind's other apps and files</strong> — anything not in the selected paths listed above.</li>
<li><strong>Your mind's git history</strong> — no commit your mind ever made is published (see the git-history note above).</li>
<li><strong>Chat file uploads</strong> — the <code>uploads/</code> directory stays behind.</li>
<li><strong>Personal values removed by the published-version modifications</strong> — see below.</li>
</ul>
{mods_html}
</section>

<section>
<h2>The manifest (<code>inspiration-{slug}.md</code>)</h2>
<p class="meta">This is the document a future mind reads to understand and adapt the inspiration. Rendered from the snapshot verbatim.</p>
{manifest_html}
</section>

</main>
<footer>This is a preview — publishing happens only after you confirm in chat.</footer>
</body>
</html>
""".format(
    title=html.escape(title),
    description=html.escape(description),
    slug=html.escape(slug),
    fmt=html.escape(fmt),
    thumbnail=html.escape(thumbnail_name),
    selected_lis="".join(selected_lis) or "<li>(none)</li>",
    generated_lis="".join(generated_lis),
    carried_html=carried_html,
    tree_lis="".join(tree_lis),
    mods_html=mods_html,
    manifest_html=render_markdown(body),
)

(out_dir / "index.html").write_text(page, encoding="utf-8")
PYEOF

echo "render_preview.sh: wrote ${OUT_DIR}/index.html and ${OUT_DIR}/${THUMBNAIL}" >&2
