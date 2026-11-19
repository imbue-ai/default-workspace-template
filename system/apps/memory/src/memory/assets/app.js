// The Memory app's page: lists the agent's remembered notes and edits them.
const KINDS = {
  user: { label: "About you", help: "Who you are and what you prefer" },
  feedback: { label: "How I should work", help: "Corrections and approaches you confirmed" },
  project: { label: "Project", help: "Ongoing work, goals and deadlines" },
  reference: { label: "Reference", help: "Where to find things: links, dashboards" },
};
const HAS_REASONS = new Set(["feedback", "project"]);

let notes = [];
let outline = { entries: [], unlisted_note_ids: [], raw: "", exists: false };
// What the right side shows: a note ("note"), the raw index ("index"), or a missing file's line ("missing").
let view = "note";
let missingEntry = null;
let tab = "edit";
let selected = null;
let filter = "all";
let query = "";
let draft = null;
let original = null;
let loadError = "";
// The agent's recorded changes, for the "changes" view, and the one a link asked to see.
let changes = [];
let focusChange = new URLSearchParams(location.search).get("change");

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const kindLabel = (k) => (KINDS[k] ? KINDS[k].label : k || "Other");
const kindClass = (k) => (KINDS[k] ? `kind-${k}` : "kind-other");

async function api(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.detail || `Something went wrong (${response.status}).`);
    error.status = response.status;
    throw error;
  }
  return data;
}

async function load(keepSelection = true) {
  try {
    const data = await api("GET", "/api/notes");
    notes = data.notes;
    outline = data.index;
    loadError = "";
  } catch (e) {
    loadError = e.message;
  }
  renderFilters();
  if (keepSelection && view === "index") return showIndex();
  if (keepSelection && view === "changes") return showChanges();
  const still = keepSelection && notes.find((n) => n.id === selected);
  select(still ? still.id : notes[0]?.id ?? null, true);
}

function isDirty() {
  if (!draft) return false;
  readForm();
  if (!original) return Boolean(draft.title || draft.summary || draft.body || draft.why || draft.how);
  return ["title", "summary", "kind", "body", "why", "how"].some((k) => draft[k] !== original[k]);
}

function confirmLeave() {
  return !isDirty() || window.confirm("You have unsaved changes to this note. Discard them?");
}

function renderFilters() {
  const opts = [["all", "All"], ...Object.entries(KINDS).map(([k, v]) => [k, v.label])];
  $("filters").innerHTML = opts
    .map(([k, l]) => `<button aria-pressed="${filter === k}" data-filter="${k}">${l}</button>`)
    .join("");
}

function noteMatches(n) {
  const q = query.toLowerCase();
  return (filter === "all" || n.kind === filter) && (!q || [n.title, n.summary, n.body, n.why, n.how].join(" ").toLowerCase().includes(q));
}

function noteRow(n, entry) {
  const lineLength = entry ? (outline.raw.split("\n")[entry.line_number - 1] || "").length : 0;
  const flag = (n.is_index_only ? `<span class="flag">Written straight into the index</span>` : "")
    + (lineLength > LINE_LIMIT ? `<span class="flag warn">Long index line (${lineLength} characters)</span>` : "");
  const title = entry && entry.text ? entry.text : n.title;
  const hook = entry && entry.hook ? entry.hook : n.summary;
  return `<button class="note-row" data-part="list-row" aria-current="${view === "note" && n.id === selected}" id="note-${esc(n.id)}" data-note-id="${esc(n.id)}">
    <span class="kind ${kindClass(n.kind)}" data-part="badge">${esc(kindLabel(n.kind))}</span>
    <span class="title">${esc(title)}</span>
    <span class="summary">${esc(hook)}</span>${flag}</button>`;
}

function renderList() {
  const narrowed = Boolean(query) || filter !== "all";
  const byId = new Map(notes.map((n) => [n.id, n]));
  // The index line by line, grouped under its headings so a heading with nothing matching can hide.
  const groups = [{ heading: null, items: [] }];
  for (const entry of outline.entries) {
    if (entry.kind === "heading") groups.push({ heading: entry, items: [] });
    else groups[groups.length - 1].items.push(entry);
  }
  let rows = 0;
  let html = `<div class="nav-index-head"><span>Your index</span><button class="nav-raw" data-variant="ghost" aria-pressed="${view === "index"}" id="show-index">${view === "index" ? "Hide raw index" : "Show raw index"}</button></div>`;
  for (const group of groups) {
    let body = "";
    for (const entry of group.items) {
      const n = byId.get(entry.note_id);
      if (n) {
        if (noteMatches(n)) { body += noteRow(n, entry); rows++; }
      } else if (entry.kind === "missing") {
        if (!narrowed || entry.text.toLowerCase().includes(query.toLowerCase())) {
          body += `<button class="note-row missing" data-part="list-row" aria-current="${view === "missing" && missingEntry?.line_number === entry.line_number}" id="index-line-${entry.line_number}" data-missing-line="${entry.line_number}">
            <span class="title">${esc(entry.text)}</span><span class="summary">${esc(entry.hook)}</span><span class="flag">Its file is missing</span></button>`;
          rows++;
        }
      } else if (!narrowed) {
        body += `<div class="nav-text">${esc(entry.text)}</div>`;
      }
    }
    if (group.heading && (body || !narrowed)) html += `<div class="nav-heading level-${group.heading.level}">${esc(group.heading.text)}</div>`;
    html += body;
  }
  const unlisted = outline.unlisted_note_ids.map((id) => byId.get(id)).filter((n) => n && noteMatches(n));
  if (unlisted.length) {
    html += `<div class="nav-heading">Not in the index</div>`;
    html += unlisted.map((n) => noteRow(n, null)).join("");
    rows += unlisted.length;
  }
  if (loadError) html = `<p class="empty-list">Couldn't load your notes: ${esc(loadError)}</p>`;
  else if (!notes.length && !outline.entries.length) html += `<p class="empty-list">I don't remember anything yet. Add a note to tell me something to keep in mind.</p>`;
  else if (narrowed && !rows) html += `<p class="empty-list">No notes match.</p>`;
  else if (!outline.exists) html += `<p class="empty-list">There is no index yet. Adding a note creates it.</p>`;
  $("note-list").innerHTML = html;
}

// A line-by-line comparison of two texts: each line marked kept, removed or added.
function lineDiff(before, after) {
  const a = (before ?? "").split("\n");
  const b = (after ?? "").split("\n");
  const table = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i--)
    for (let j = b.length - 1; j >= 0; j--)
      table[i][j] = a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { out.push(["same", a[i]]); i++; j++; }
    else if (table[i + 1][j] >= table[i][j + 1]) out.push(["removed", a[i++]]);
    else out.push(["added", b[j++]]);
  }
  while (i < a.length) out.push(["removed", a[i++]]);
  while (j < b.length) out.push(["added", b[j++]]);
  return out;
}

function fileChange(f) {
  const what = f.before === null ? "Added" : f.after === null ? "Deleted" : "Changed";
  const lines = f.before === null ? lineDiff("", f.after).filter(([k]) => k !== "removed")
    : f.after === null ? lineDiff(f.before, "").filter(([k]) => k !== "added")
    : lineDiff(f.before, f.after);
  const marks = { same: " ", removed: "-", added: "+" };
  // Unchanged lines more than one away from a change fold into a count, so the change itself shows.
  const near = (i) => [i - 1, i, i + 1].some((x) => lines[x] && lines[x][0] !== "same");
  let html = "";
  let folded = 0;
  lines.forEach(([k, l], i) => {
    if (k === "same" && !near(i)) { folded++; return; }
    if (folded) { html += `<span class="diff-fold">${folded} unchanged line${folded === 1 ? "" : "s"}</span>`; folded = 0; }
    html += `<span class="diff-${k}">${marks[k]} ${esc(l) || " "}</span>`;
  });
  if (folded) html += `<span class="diff-fold">${folded} unchanged line${folded === 1 ? "" : "s"}</span>`;
  const label = f.name === "MEMORY.md" ? "the index (MEMORY.md)" : f.name;
  return `<div class="change-file"><span class="help">${what} ${esc(label)}</span><pre class="diff">${html}</pre></div>`;
}

function changeCard(c) {
  const when = new Date(c.time).toLocaleString();
  const state = c.undone_at
    ? `<span class="help">Undone ${esc(new Date(c.undone_at).toLocaleString())}</span>`
    : `<button data-undo="${esc(c.id)}">Undo</button>`;
  return `<section class="change-card ${c.undone_at ? "undone" : ""}" id="change-${esc(c.id)}" aria-current="${c.id === focusChange}">
    <div class="row"><h3>${esc(c.summary.charAt(0).toUpperCase() + c.summary.slice(1))}</h3>${state}</div>
    <span class="help">${esc(when)}${c.agent_name ? ` in the chat "${esc(c.agent_name)}"` : ""}</span>
    <span class="status error" id="undo-status-${esc(c.id)}"></span>
    ${c.files.map(fileChange).join("")}
  </section>`;
}

async function showChanges(force = true) {
  if (!force && !confirmLeave()) return;
  view = "changes";
  draft = null;
  original = null;
  renderList();
  $("show-changes").setAttribute("aria-pressed", "true");
  try {
    changes = (await api("GET", "/api/changes")).changes;
  } catch (e) {
    $("editor").innerHTML = `<p class="blank">Couldn't load the changes: ${esc(e.message)}</p>`;
    return;
  }
  $("editor").innerHTML = `<div class="editor-inner raw-view">
    <div class="editor-toolbar"><span class="help">Press Esc to go back to your notes.</span><button id="hide-changes">Back to your notes</button></div>
    <div class="row"><h2>Recent changes by the agent</h2></div>
    <span class="help">Every time the agent saves, edits or deletes something in your memory, it shows up here. Undo puts the files back as they were before that change.</span>
    ${changes.length ? changes.map(changeCard).join("") : `<p class="blank">The agent hasn't changed your memory since this list started.</p>`}
  </div>`;
  if (focusChange) $(`change-${focusChange}`)?.scrollIntoView({ block: "center" });
}

function hideChanges() {
  $("show-changes").setAttribute("aria-pressed", "false");
  hideIndex();
}

async function undoChange(id, force = false) {
  try {
    await api("POST", `/api/changes/${encodeURIComponent(id)}/undo`, { force });
  } catch (e) {
    if (e.status === 409 && window.confirm(`${e.message} Undo anyway?`)) return undoChange(id, true);
    const status = $(`undo-status-${id}`);
    if (status) status.textContent = e.status === 409 ? "Not undone." : e.message;
    return;
  }
  const data = await api("GET", "/api/notes");
  notes = data.notes;
  outline = data.index;
  focusChange = id;
  showChanges();
}

const rawLabel = (n) => `Show raw ${n.is_index_only ? "index line" : "file"}`;

// Leave the raw index for the note that was open before it (or the first note).
function hideIndex() {
  const back = notes.find((n) => n.id === selected) || notes[0];
  view = "note";
  if (back) select(back.id, true);
  else { draft = null; original = null; renderList(); renderEditor(); }
}

function showIndex(force = true) {
  if (!force && !confirmLeave()) return;
  view = "index";
  draft = null;
  original = null;
  renderList();
  $("editor").innerHTML = `<div class="editor-inner raw-view">
    <div class="editor-toolbar"><span class="help">Read only. Press Esc to go back to your notes.</span><button id="hide-index">Back to your notes</button></div>
    <div class="row"><h2>Your index</h2><span class="help">${esc(outline.raw.split("\n").filter((l) => l.trim()).length)} lines, loaded at the start of every chat</span></div>
    <span class="help">Saved as MEMORY.md. The list on the left follows it line by line.</span>
    <pre id="raw-index">${outline.exists ? esc(outline.raw) : "There is no index yet."}</pre>
  </div>`;
}

function showMissing(lineNumber) {
  if (!confirmLeave()) return;
  view = "missing";
  draft = null;
  original = null;
  missingEntry = outline.entries.find((e) => e.line_number === lineNumber);
  renderList();
  $("editor").innerHTML = `<div class="editor-inner raw-view">
    <h2>${esc(missingEntry.text)}</h2>
    <p class="notice">Your index lists this note, but its file (${esc(missingEntry.file_name)}) doesn't exist. I see the title at the start of a chat but nothing behind it.</p>
    <span class="help">The index line, as saved (line ${missingEntry.line_number}):</span>
    <pre>${esc(outline.raw.split("\n")[missingEntry.line_number - 1] || "")}</pre>
  </div>`;
}

function linkChips(n) {
  const names = [...new Set((`${n.body}\n${n.why}\n${n.how}`.match(/\[\[([^\]\n]+)\]\]/g) || []).map((m) => m.slice(2, -2).trim()))];
  if (!names.length) return `<span class="help">None. To link another note, write its link name in double square brackets, like [[${esc(notes.find((x) => x.name)?.name || "note-name")}]].</span>`;
  return names
    .map((l) => {
      const t = notes.find((x) => x.name === l);
      return t
        ? `<button data-goto="${esc(t.id)}">${esc(t.title)}</button>`
        : `<span class="link-missing" data-part="badge" title="No note has this link name yet">${esc(l)} (not written yet)</span>`;
    })
    .join("");
}

function renderEditor(status = "", isError = false) {
  if (!draft) {
    $("editor").innerHTML = `<p class="blank">${notes.length ? "Pick a note on the left, or add a new one." : "Click New note to add the first thing I should remember."}</p>`;
    return;
  }
  const n = draft;
  const isNew = !original;
  const showReasons = HAS_REASONS.has(n.kind) || n.why || n.how;
  // The same small toggle as the sidebar's "Show raw index": pressed while the raw file shows, pressed again to go back.
  const tabs = original
    ? `<div class="editor-toolbar">${tab === "raw" ? `<span class="help">Read only. Press Esc to go back to editing.</span>` : ""}${tab === "raw" ? `<button id="show-raw-file" data-tab="edit">Back to editing</button>` : `<button class="nav-raw" data-variant="ghost" aria-pressed="false" id="show-raw-file" data-tab="raw">${rawLabel(original)}</button>`}</div>`
    : "";
  if (original && tab === "raw") {
    $("editor").innerHTML = `<div class="editor-inner raw-view">${tabs}
      <div class="row"><h2>${esc(original.file_name || "MEMORY.md")}</h2><span class="help">${original.is_index_only ? "This note is one line of the index." : "Exactly as saved."}</span></div>
      <pre id="raw-file">${esc(original.raw)}</pre></div>`;
    return;
  }
  $("editor").innerHTML = `<div class="editor-inner">${tabs}
    ${original?.is_index_only ? `<p class="notice">This note is written straight into my index rather than saved on its own. Saving it keeps it where it is and gives it its own page, so it can carry a summary and links.</p>` : ""}
    <div class="field"><input id="title-input" value="${esc(n.title)}" placeholder="Give this note a title" aria-label="Title"><span class="check error" id="title-check"></span></div>
    <div class="field"><label>Kind</label><div class="kind-picker">
      ${Object.entries(KINDS).map(([k, v]) => `<button aria-pressed="${n.kind === k}" data-kind="${k}"><span class="kind kind-${k}" data-part="badge">${v.label}</span><small>${v.help}</small></button>`).join("")}
    </div></div>
    <div class="field"><label for="summary-input">One-line summary</label><span class="help">I read this to decide whether the note matters for a chat.</span><input id="summary-input" value="${esc(n.summary)}"><span class="check warn" id="line-check"></span></div>
    <div class="field"><label for="body-input">What I should remember</label><textarea id="body-input">${esc(n.body)}</textarea></div>
    ${showReasons ? `
    <div class="field"><label for="why-input">Why</label><textarea id="why-input" class="short">${esc(n.why)}</textarea></div>
    <div class="field"><label for="how-input">How I should apply it</label><textarea id="how-input" class="short">${esc(n.how)}</textarea></div>` : ""}
    <div class="field"><label>Related notes</label><div class="links">${linkChips(n)}</div></div>
    <div class="actions">
      <button id="save" data-variant="primary">${isNew ? "Add note" : "Save changes"}</button>
      <button id="discard">Discard changes</button>
      <span class="status ${isError ? "error" : ""}" id="status">${status}</span>
      ${isNew ? "" : `<button id="delete" data-variant="ghost" class="delete">Delete note</button>`}
    </div>
    ${original ? `<div class="meta">${original.name ? `<span>Link name: ${esc(original.name)}</span>` : ""}<span>${original.file_name ? `Saved as ${esc(original.file_name)}` : "Saved in the index"}</span><span id="rename-note"></span></div>` : ""}
  </div>`;
  updateChecks();
}

// Mirrors the store's slugify: lowercase words joined by hyphens, at most eight.
const slugify = (title) => title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").split("-").slice(0, 8).join("-") || "note";
const oneLine = (text) => text.split(/\s+/).filter(Boolean).join(" ");
const LINE_LIMIT = 150;

// The link name a save would give the note: a new title renames it, an unchanged one keeps it.
function plannedName(title) {
  if (original && !original.is_index_only && oneLine(title) === original.title) return original.name;
  const base = slugify(title);
  if (original && original.id === base) return base;
  const taken = new Set(notes.filter((x) => x.file_name && x.id !== original?.id).map((x) => x.id));
  let name = base;
  for (let i = 2; taken.has(name); i++) name = `${base}-${i}`;
  return name;
}

// Live checks under the title and summary: brackets block saving, a long index line only warns.
function updateChecks() {
  const titleInput = $("title-input");
  if (!titleInput) return;
  const title = titleInput.value;
  const summary = $("summary-input").value;
  const hasBrackets = /[[\]]/.test(title);
  $("title-check").textContent = hasBrackets ? "Titles can't contain square brackets. Remove them to save." : "";
  $("save").disabled = hasBrackets;
  const name = plannedName(title);
  const length = `- [${oneLine(title)}](${name}.md) — ${oneLine(summary)}`.length;
  $("line-check").textContent = length > LINE_LIMIT
    ? `This note's index line would be ${length} characters. Claude Code suggests keeping it under about ${LINE_LIMIT}, so try a shorter title or summary. You can still save.`
    : "";
  const rename = $("rename-note");
  if (rename) {
    rename.textContent = original && !original.is_index_only && oneLine(title) && name !== original.name
      ? `Saving renames it to ${name}.md and updates links to it.`
      : "";
  }
}

function select(id, force = false) {
  if (!force && !confirmLeave()) return;
  if (id !== selected || view !== "note") tab = "edit";
  view = "note";
  selected = id;
  original = notes.find((x) => x.id === id) || null;
  draft = original ? { ...original, kind: KINDS[original.kind] ? original.kind : "user" } : null;
  renderList();
  renderEditor();
}

function readForm() {
  if (!draft || !$("title-input")) return;
  draft.title = $("title-input").value;
  draft.summary = $("summary-input").value;
  draft.body = $("body-input").value;
  if ($("why-input")) draft.why = $("why-input").value;
  if ($("how-input")) draft.how = $("how-input").value;
}

function showError(e) {
  if (e.status === 409 || e.status === 404) {
    renderEditor(`${esc(e.message)} <button class="reload" data-variant="ghost" id="reload">Reload</button>`, true);
  } else {
    renderEditor(esc(e.message), true);
  }
}

async function save() {
  readForm();
  const content = { title: draft.title, summary: draft.summary, kind: draft.kind, body: draft.body, why: draft.why || "", how: draft.how || "" };
  $("save").disabled = true;
  try {
    const saved = original
      ? await api("PUT", `/api/notes/${encodeURIComponent(original.id)}`, { version: original.version, note: content })
      : await api("POST", "/api/notes", content);
    await load(false);
    select(saved.id, true);
    renderEditor("Saved. I'll use it from your next chat.");
  } catch (e) {
    showError(e);
  }
}

async function remove() {
  try {
    await api("POST", `/api/notes/${encodeURIComponent(original.id)}/delete`, { version: original.version });
    selected = null;
    await load(false);
  } catch (e) {
    showError(e);
  }
}

document.addEventListener("click", (e) => {
  const t = e.target.closest("button");
  if (!t) return;
  if (t.dataset.filter) {
    filter = t.dataset.filter;
    renderFilters();
    renderList();
  } else if (t.dataset.noteId) select(t.dataset.noteId);
  else if (t.dataset.goto) select(t.dataset.goto);
  else if (t.dataset.missingLine) showMissing(Number(t.dataset.missingLine));
  else if (t.id === "show-index") view === "index" ? hideIndex() : showIndex(false);
  else if (t.id === "hide-index") hideIndex();
  else if (t.id === "show-changes") view === "changes" ? hideChanges() : showChanges(false);
  else if (t.id === "hide-changes") hideChanges();
  else if (t.dataset.undo) { t.disabled = true; undoChange(t.dataset.undo); }
  else if (t.dataset.tab) {
    readForm();
    tab = t.dataset.tab;
    renderEditor();
  }
  else if (t.dataset.kind) {
    readForm();
    draft.kind = t.dataset.kind;
    renderEditor();
  } else if (t.id === "new-note") {
    if (!confirmLeave()) return;
    selected = null;
    view = "note";
    tab = "edit";
    original = null;
    draft = { title: "", summary: "", kind: "user", body: "", why: "", how: "" };
    renderList();
    renderEditor();
    $("title-input").focus();
  } else if (t.id === "save") save();
  else if (t.id === "discard") select(selected, true);
  else if (t.id === "reload") load(true);
  else if (t.id === "delete") {
    const back = document.createElement("div");
    back.className = "modal-back";
    back.innerHTML = `<div class="modal"><p>Delete "${esc(original.title)}"? I will stop remembering it.</p><div class="row"><button id="cancel-delete">Keep it</button><button data-variant="destructive" id="confirm-delete">Delete note</button></div></div>`;
    document.body.append(back);
  } else if (t.id === "cancel-delete") t.closest(".modal-back").remove();
  else if (t.id === "confirm-delete") {
    t.closest(".modal-back").remove();
    remove();
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || document.querySelector(".modal-back")) return;
  if (view === "index") hideIndex();
  else if (view === "changes") hideChanges();
  else if (view === "note" && tab === "raw") { tab = "edit"; renderEditor(); }
});
document.addEventListener("input", (e) => {
  if (e.target.id === "title-input" || e.target.id === "summary-input") updateChecks();
});
$("search").addEventListener("input", (e) => {
  query = e.target.value;
  renderList();
});
// Pick up notes the agent wrote while the window was in the background.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && !isDirty()) load(true);
});

load(false).then(() => { if (focusChange) showChanges(); });
