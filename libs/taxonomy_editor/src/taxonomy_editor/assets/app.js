"use strict";

/* Concept Taxonomy — lexicographer's workbench.
 * Single-page editor. Every field of every record is directly editable; edits
 * mutate the in-memory record and autosave (debounced) to the backend, which
 * persists the working copy under runtime/. The right pane surfaces the raw,
 * unedited vault docs the taxonomy was built from. */

const state = {
  data: null,
  selected: null, // { kind: 'concept'|'cluster', id }
  search: "",
  filters: { facing: null, scope: null, decided: null }, // null = off
  viewMode: "grouped", // "grouped" hides member concepts under their groups; "all" is the flat list
  collapsed: new Set(),
  saveTimers: new Map(),
};

/* A concept is a "member" if some group nests it via rolls_up. Derived live from
 * the structure (not a stored list) so linking/unlinking a member in the UI —
 * which edits a group's rolls_up and is persisted — updates hiding automatically. */
const memberSet = () => {
  const s = new Set();
  for (const c of state.data.concepts) {
    if (c.kind === "group") for (const m of c.rolls_up || []) if (m.concept_id) s.add(m.concept_id);
  }
  return s;
};
function addMemberLink(group, conceptId) {
  const x = state.data.concepts.find((c) => c.id === conceptId);
  if (!x) return;
  group.rolls_up = group.rolls_up || [];
  if (group.rolls_up.some((m) => m.concept_id === conceptId)) return;
  group.rolls_up.push({ term: x.canonical_term, code_term: "", note: "", scope: x.scope || "minds", concept_id: conceptId });
}

/* The "core code term" of a group is the single member it maps to most directly.
 * Three states, resolved at the group level (like routing an overloaded word):
 *   - "resolved"   : one member is marked core
 *   - "collection" : explicitly no single core (group.no_single_core)
 *   - "unresolved" : neither — needs a decision */
const groupCoreMember = (g) => (g.rolls_up || []).find((m) => m.core) || null;
function coreState(g) {
  if (g.kind !== "group") return null;
  if (groupCoreMember(g)) return "resolved";
  if (g.no_single_core) return "collection";
  return "unresolved";
}
function setGroupCore(g, memberConceptId) {
  (g.rolls_up || []).forEach((m) => delete m.core);
  const m = (g.rolls_up || []).find((mm) => mm.concept_id === memberConceptId);
  if (m) {
    m.core = true;
    g.no_single_core = false;
  }
  (g.rolls_up || []).sort((a, b) => (a.core ? 0 : 1) - (b.core ? 0 : 1));
}
function markGroupCollection(g, isCollection) {
  if (isCollection) (g.rolls_up || []).forEach((m) => delete m.core);
  g.no_single_core = isCollection;
}

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

/* ------------------------------------------------------------------ load */
async function boot() {
  const r = await fetch("api/data");
  state.data = await r.json();
  renderFilters();
  renderCounts();
  renderIndex();
  document.getElementById("search").addEventListener("input", (e) => {
    state.search = e.target.value.toLowerCase();
    renderIndex();
  });
  document.getElementById("exportBtn").addEventListener("click", () => {
    window.open("api/export", "_blank");
  });
  document.getElementById("sourceToggle").addEventListener("click", () => setDrawer());
  document.getElementById("closeDrawer").addEventListener("click", () => setDrawer(false));
  // open the source drawer by default only when there's room for it
  setDrawer(window.innerWidth > 1100);
}

function setDrawer(open) {
  const pane = document.getElementById("sourcePane");
  const btn = document.getElementById("sourceToggle");
  const next = open === undefined ? !pane.classList.contains("open") : open;
  pane.classList.toggle("open", next);
  btn.classList.toggle("on", next);
}

/* ------------------------------------------------------------------ saving */
function scheduleSave(kind, obj) {
  setSaveState("saving");
  const key = kind + ":" + obj.id;
  if (state.saveTimers.has(key)) clearTimeout(state.saveTimers.get(key));
  state.saveTimers.set(
    key,
    setTimeout(async () => {
      try {
        await fetch("api/" + kind + "/" + encodeURIComponent(obj.id), {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(obj),
        });
        setSaveState("saved");
      } catch (err) {
        setSaveState("error");
      }
    }, 500),
  );
}
let saveStateTimer = null;
function setSaveState(s) {
  const node = document.getElementById("saveState");
  node.className = "save-state " + s;
  node.textContent = s === "saving" ? "saving…" : s === "saved" ? "saved ✓" : s === "error" ? "save failed" : "·";
  if (s === "saved") {
    clearTimeout(saveStateTimer);
    saveStateTimer = setTimeout(() => {
      node.textContent = "·";
      node.className = "save-state";
    }, 1800);
  }
}

/* ------------------------------------------------------------------ status helpers */
const conceptDecided = (c) => !!(c.decision && c.decision.decided) && !c.to_delete;
const clusterDecided = (c) => !!(c.decision && c.decision.decided) && !c.to_delete;
const toDelete = (c) => !!c.to_delete;

function renderCounts() {
  const all = [...state.data.concepts, ...state.data.clusters];
  const decided = all.filter((c) => conceptDecided(c)).length;
  const del = all.filter(toDelete).length;
  const open = all.length - decided - del;
  const box = document.getElementById("counts");
  box.innerHTML = "";
  const tally = (cls, n, label) => {
    const t = el("span", "tally");
    t.append(el("span", "dot " + cls), document.createTextNode(`${n} ${label}`));
    return t;
  };
  box.append(tally("settled", decided, "decided"));
  if (del) box.append(tally("delete", del, "to delete"));
  box.append(tally("pen", open, "open"));
}

/* ------------------------------------------------------------------ filters */
const FILTERS = [
  { group: "facing", opts: [["user-facing", "user"], ["internal", "internal"]] },
  { group: "scope", opts: [["minds", "minds"], ["mngr-inherited", "mngr"]] },
  { group: "decided", opts: [["open", "open"], ["decided", "decided"], ["to delete", "todelete"]] },
];
function renderFilters() {
  const box = document.getElementById("filters");
  box.innerHTML = "";
  // view-mode toggle: grouped (members nested under groups) vs all terms (flat)
  for (const [label, val] of [["grouped", "grouped"], ["all terms", "all"]]) {
    const chip = el("span", "fchip view", label);
    if (state.viewMode === val) chip.classList.add("on");
    chip.onclick = () => { state.viewMode = val; renderFilters(); renderIndex(); };
    box.append(chip);
  }
  box.append(el("span", "fsep"));
  for (const f of FILTERS) {
    for (const [label, val] of f.opts) {
      const chip = el("span", "fchip", label);
      if (state.filters[f.group] === val) chip.classList.add("on");
      chip.onclick = () => {
        state.filters[f.group] = state.filters[f.group] === val ? null : val;
        renderFilters();
        renderIndex();
      };
      box.append(chip);
    }
  }
}

/* the facing/scope/decided/search filters — membership is handled separately */
function passesFilters(c) {
  const f = state.filters;
  if (f.facing === "user" && !c.user_facing) return false;
  if (f.facing === "internal" && c.user_facing) return false;
  if (f.scope === "minds" && c.scope !== "minds") return false;
  if (f.scope === "mngr" && c.scope !== "mngr-inherited") return false;
  if (f.decided === "decided" && !conceptDecided(c)) return false;
  if (f.decided === "open" && (conceptDecided(c) || toDelete(c))) return false;
  if (f.decided === "todelete" && !toDelete(c)) return false;
  if (state.search && !JSON.stringify(c).toLowerCase().includes(state.search)) return false;
  return true;
}

/* linked child concepts of a group, in rolls_up order */
function groupChildren(c) {
  if (c.kind !== "group") return [];
  return (c.rolls_up || []).map((m) => (m.concept_id ? state.data.concepts.find((x) => x.id === m.concept_id) : null)).filter(Boolean);
}

/* a concept shows if it (or, in the tree, any descendant) passes the filters */
function subtreeMatches(c) {
  if (passesFilters(c)) return true;
  return groupChildren(c).some(subtreeMatches);
}

/* top-level rows: concepts not nested in a group. Grouped view still surfaces
 * group concepts at top level (you can always find a group); all-terms nests them. */
function isTopLevel(c) {
  if (!memberSet().has(c.id)) return true;
  return state.viewMode === "grouped" && c.kind === "group";
}

/* ------------------------------------------------------------------ index pane */
function renderIndex() {
  const list = document.getElementById("indexList");
  list.innerHTML = "";
  const groupsById = Object.fromEntries(state.data.groups.map((g) => [g.id, g]));
  const byGroup = {};
  for (const c of state.data.concepts) {
    if (!isTopLevel(c) || !subtreeMatches(c)) continue;
    (byGroup[c.group] = byGroup[c.group] || []).push(c);
  }
  for (const g of state.data.groups) {
    const items = byGroup[g.id];
    if (!items || !items.length) continue;
    list.append(buildGroup(g.id, g.title, items, "concept"));
  }
  // cross-cutting clusters
  const clusters = state.data.clusters.filter((cl) => {
    if (state.filters.facing) return false; // clusters are word-level, skip facing filter
    if (state.filters.scope === "minds" && cl.scope !== "minds") return false;
    if (state.filters.scope === "mngr" && cl.scope !== "mngr-inherited") return false;
    if (state.filters.decided === "decided" && !clusterDecided(cl)) return false;
    if (state.filters.decided === "open" && (clusterDecided(cl) || toDelete(cl))) return false;
    if (state.filters.decided === "todelete" && !toDelete(cl)) return false;
    if (state.search && !JSON.stringify(cl).toLowerCase().includes(state.search)) return false;
    return true;
  });
  if (clusters.length) list.append(buildGroup("__clusters", "Cross-cutting — overloaded words", clusters, "cluster"));
}

function buildGroup(gid, title, items, kind) {
  const wrap = el("div", "group");
  const head = el("div", "group-head");
  if (state.collapsed.has(gid)) head.classList.add("collapsed");
  head.append(el("span", "caret", "▼"), el("span", null, title), el("span", "gcount", String(items.length)));
  head.onclick = () => {
    if (state.collapsed.has(gid)) state.collapsed.delete(gid);
    else state.collapsed.add(gid);
    renderIndex();
  };
  wrap.append(head);
  if (!state.collapsed.has(gid)) {
    const tree = kind === "concept" && state.viewMode === "all";
    for (const it of items) wrap.append(tree ? buildTreeNode(it) : buildRow(it, kind));
  }
  return wrap;
}

/* all-terms view: render a group concept with its linked members nested beneath
 * it (recursively) inside an indented, rail-bordered container so the hierarchy
 * reads clearly. */
function buildTreeNode(c, isCore) {
  const node = el("div", "tnode");
  const row = buildRow(c, "concept");
  if (isCore) row.querySelector(".rname").append(el("span", "core-pip", " ★"));
  const isGroup = c.kind === "group" && (c.rolls_up || []).length;
  const key = "node:" + c.id;
  const collapsed = state.collapsed.has(key);
  const caret = el("span", "tcaret", isGroup ? (collapsed ? "▸" : "▾") : "");
  if (isGroup) {
    row.classList.add("tgroup");
    caret.onclick = (e) => { e.stopPropagation(); if (collapsed) state.collapsed.delete(key); else state.collapsed.add(key); renderIndex(); };
  }
  row.prepend(caret);
  node.append(row);
  if (isGroup && !collapsed) {
    const kids = el("div", "tchildren");
    for (const m of c.rolls_up) {
      const linked = m.concept_id ? state.data.concepts.find((x) => x.id === m.concept_id) : null;
      if (linked) { if (subtreeMatches(linked)) kids.append(buildTreeNode(linked, !!m.core)); }
      else kids.append(buildInlineMemberRow(m));
    }
    node.append(kids);
  }
  return node;
}

/* an inline (non-concept) member of a group — a layer with no record of its own */
function buildInlineMemberRow(m) {
  const row = el("div", "row inline-member");
  row.append(el("span", "tcaret", ""));
  row.append(el("span", "sdot inline"));
  row.append(el("span", "rname", m.term || "(layer)"));
  return row;
}

function buildRow(it, kind) {
  const decided = kind === "concept" ? conceptDecided(it) : clusterDecided(it);
  const del = toDelete(it);
  const row = el("div", "row" + (kind === "cluster" ? " cluster" : "") + (it.user_facing === false ? " internal" : "") + (del ? " to-delete" : ""));
  if (state.selected && state.selected.kind === kind && state.selected.id === it.id) row.classList.add("active");
  row.append(el("span", "sdot " + (del ? "delete" : decided ? "decided" : "undecided")));
  const name = el("span", "rname");
  const displayName = kind === "concept" ? it.canonical_term || it.id : it.word || it.id;
  name.textContent = displayName;
  const contested = (it.status || "").includes("contest") || /⚠/.test(JSON.stringify(it.candidate_terms || []));
  if (kind === "concept" && (it.candidate_terms || []).length > 1 && !decided) {
    const w = el("span", "warn", " ⚠");
    name.append(w);
  }
  // Render the headline (a question for groups) beneath the term; tooltip everywhere.
  if (kind === "concept" && it.headline) row.title = it.headline;
  if (kind === "concept" && it.kind === "group" && it.headline) {
    const rtext = el("div", "rtext");
    rtext.append(name, el("span", "rhead", it.headline));
    row.append(rtext);
  } else {
    row.append(name);
  }
  if (kind === "concept" && coreState(it) === "unresolved") {
    name.append(el("span", "core-warn", " ◌"));
  }
  if (kind === "concept" && it.code_term) row.append(el("span", "rcode", it.code_term));
  const memberCount = kind === "cluster" ? (it.distinct_meanings || []).length : (it.rolls_up || []).length;
  if (memberCount && (kind === "cluster" || it.kind === "group")) {
    row.classList.add("is-group");
    row.append(el("span", "group-badge", "❖ " + memberCount));
  } else if (kind === "concept" && it.scope === "mngr-inherited" && !it.code_term) {
    row.append(el("span", "rtag", "mngr"));
  }
  row.onclick = () => selectItem(kind, it.id);
  return row;
}

function selectItem(kind, id) {
  state.selected = { kind, id };
  renderIndex();
  renderEditor();
  renderSource();
}

/* ------------------------------------------------------------------ field builders */
function field(labelText, control, note) {
  const f = el("div", "field");
  if (labelText) {
    const l = el("label");
    l.append(document.createTextNode(labelText));
    if (note) l.append(el("span", "label-note", "  " + note));
    f.append(l);
  }
  f.append(control);
  return f;
}

function textInput(value, onInput, opts = {}) {
  const i = el(opts.textarea ? "textarea" : "input", "t" + (opts.mono ? " mono" : ""));
  if (!opts.textarea) i.type = "text";
  i.value = value == null ? "" : value;
  if (opts.placeholder) i.placeholder = opts.placeholder;
  i.addEventListener("input", () => onInput(i.value));
  return i;
}

function selectInput(value, options, onChange) {
  const s = el("select", "t");
  for (const opt of options) {
    const o = el("option", null, opt);
    o.value = opt;
    if (opt === value) o.selected = true;
    s.append(o);
  }
  s.addEventListener("change", () => onChange(s.value));
  return s;
}

function toggle(checked, label, onChange) {
  const wrap = el("div", "decided-toggle");
  const sw = el("label", "switch");
  const inp = el("input");
  inp.type = "checkbox";
  inp.checked = !!checked;
  inp.addEventListener("change", () => onChange(inp.checked));
  sw.append(inp, el("span", "slider"));
  wrap.append(sw, el("span", "decided-label", label));
  return wrap;
}

/* editable list of plain strings */
function stringList(arr, onMutate, opts = {}) {
  const wrap = el("div", "list-rows");
  arr.forEach((val, idx) => {
    const row = el("div", "lrow");
    if (opts.adopt) {
      const a = el("button", "adopt-btn", "↑");
      a.title = "Adopt as the canonical term";
      a.onclick = () => opts.adopt(arr[idx]);
      row.append(a);
    } else {
      row.append(el("span", "grip", "—"));
    }
    const fields = el("div", "fields");
    fields.append(
      textInput(val, (v) => { arr[idx] = v; onMutate(false); }, { mono: opts.mono, textarea: opts.textarea, placeholder: opts.placeholder }),
    );
    row.append(fields);
    if (opts.jumpKind) {
      const j = el("button", "x-btn", "↗");
      j.title = "Open " + val;
      j.onclick = () => jumpTo(opts.jumpKind, val);
      row.append(j);
    }
    const x = el("button", "x-btn", "×");
    x.onclick = () => { arr.splice(idx, 1); onMutate(true); };
    row.append(x);
    wrap.append(row);
  });
  const add = el("button", "add-btn", "+ add");
  add.onclick = () => { arr.push(""); onMutate(true); };
  const container = el("div");
  container.append(wrap, add);
  return container;
}

/* editable list of objects with a fixed schema */
function objectList(arr, schema, onMutate, layoutClass) {
  const wrap = el("div", "list-rows");
  arr.forEach((obj, idx) => {
    const row = el("div", "lrow " + (layoutClass || ""));
    row.append(el("span", "grip", "—"));
    const fields = el("div", "fields");
    for (const sf of schema) {
      const cell = el("div");
      cell.append(el("span", "mini-label", sf.label || sf.key));
      let control;
      if (sf.options) {
        control = selectInput(obj[sf.key] || sf.options[0], sf.options, (v) => { obj[sf.key] = v; onMutate(false); });
      } else {
        control = textInput(obj[sf.key], (v) => { obj[sf.key] = v; onMutate(false); }, { mono: sf.mono, textarea: sf.textarea, placeholder: sf.placeholder });
      }
      if (sf.full) cell.style.gridColumn = "1 / -1";
      cell.append(control);
      fields.append(cell);
    }
    row.append(fields);
    const x = el("button", "x-btn", "×");
    x.onclick = () => { arr.splice(idx, 1); onMutate(true); };
    row.append(x);
    wrap.append(row);
  });
  const add = el("button", "add-btn", "+ add");
  add.onclick = () => { arr.push(Object.fromEntries(schema.map((s) => [s.key, ""]))); onMutate(true); };
  const container = el("div");
  container.append(wrap, add);
  return container;
}

/* editable list of grouped MEMBERS: each carries its own scope. mngr-inherited
 * members render read-only (off-limits to rename); minds members are editable.
 * A per-row scope toggle lets you reclassify. */
function memberList(arr, schema, onMutate, layoutClass) {
  const wrap = el("div", "list-rows");
  arr.forEach((obj, idx) => {
    const locked = obj.scope === "mngr-inherited";
    const row = el("div", "lrow member " + (layoutClass || "") + (locked ? " locked" : ""));
    const fields = el("div", "fields");
    for (const sf of schema) {
      const cell = el("div");
      cell.append(el("span", "mini-label", sf.label || sf.key));
      // mngr members stay editable (you may still want to tweak them) but are
      // visibly marked off-limits via the row styling + scope toggle.
      const control = textInput(obj[sf.key], (val) => { obj[sf.key] = val; onMutate(false); }, { mono: sf.mono, textarea: sf.textarea, placeholder: sf.placeholder });
      if (sf.full) cell.style.gridColumn = "1 / -1";
      cell.append(control);
      fields.append(cell);
    }
    row.append(fields);
    const sc = el("button", "scope-toggle " + (locked ? "mngr" : "minds"), locked ? "🔒 mngr" : "minds");
    sc.title = locked ? "Off-limits (from mngr). Click to make editable." : "In scope (minds). Click to mark off-limits (mngr).";
    sc.onclick = () => { obj.scope = locked ? "minds" : "mngr-inherited"; onMutate(true); };
    row.append(sc);
    const x = el("button", "x-btn", "×");
    x.onclick = () => { arr.splice(idx, 1); onMutate(true); };
    row.append(x);
    wrap.append(row);
  });
  const add = el("button", "add-btn", "+ add member");
  add.onclick = () => { arr.push(Object.assign(Object.fromEntries(schema.map((s) => [s.key, ""])), { scope: "minds" })); onMutate(true); };
  const container = el("div");
  container.append(wrap, add);
  return container;
}

function blockTitle(text, hint) {
  const t = el("div", "block-title");
  t.append(document.createTextNode(text));
  if (hint) t.append(el("span", "hint", hint));
  return t;
}

function jumpTo(kind, id) {
  const coll = kind === "cluster" ? state.data.clusters : state.data.concepts;
  if (coll.some((x) => x.id === id)) selectItem(kind, id);
}

/* Two distinct names. The `headline` is the user-facing framing — a question
 * for a group (answered by its members) or a plain statement for a leaf term.
 * The `canonical_term` is always a noun: the glossary headword and the
 * candidate name for a code refactor — never a question or statement. (The
 * headline is separate from the `definition`, which is the precise meaning.) */
function nameFields(c, onSave) {
  const isGroup = c.kind === "group";
  const fields = [];
  fields.push(
    field(
      "Headline",
      textInput(c.headline, (v) => { c.headline = v; onSave(false); }, {
        placeholder: isGroup ? "the question this answers" : "a one-line plain-language statement",
      }),
      isGroup ? "a question, answered by this group's members" : "a short user-facing statement",
    ),
  );
  fields.push(
    field(
      "Canonical term",
      textInput(c.canonical_term, (v) => { c.canonical_term = v; onSave(false); }, { mono: true, placeholder: "a noun — the one true term" }),
      "the glossary term — always a noun, never a question or statement",
    ),
  );
  fields.push(
    field(
      "Code term",
      textInput(c.code_term, (v) => { c.code_term = v; onSave(false); }, { mono: true, placeholder: "the current code symbol, e.g. AIProvider" }),
      "the symbol/type this maps to in code today — one per term (split the concept if there are several)",
    ),
  );
  return fields;
}

/* a compact editor for a member concept, shown inline inside its group */
function renderCompactConcept(c) {
  const box = el("div", "compact-concept");
  c.decision = c.decision || { decided: false, notes: "" };
  box.append(toggle(c.user_facing, c.user_facing ? "shown to users" : "internal only", (v) => { c.user_facing = v; saveConcept(c, true); }));
  for (const f of nameFields(c, (r) => saveConcept(c, r))) box.append(f);
  box.append(field("Definition (plain language)", textInput(c.working_definition, (v) => { c.working_definition = v; saveConcept(c, false); }, { textarea: true })));
  box.append(field("Definition (technical)", textInput(c.technical_definition, (v) => { c.technical_definition = v; saveConcept(c, false); }, { textarea: true })));
  c.enum_values = c.enum_values || [];
  box.append(field("Values", objectList(c.enum_values, [{ key: "value", label: "value", mono: true }, { key: "note", label: "meaning" }], (r) => saveConcept(c, r), "cols-2 enum-rows")));
  box.append(toggle(c.decision.decided, c.decision.decided ? "decided" : "mark decided", (v) => { c.decision.decided = v; saveConcept(c, true); }));
  return box;
}

/* Resolving a group's core code term, mirroring overloaded-word resolution:
 * pick the one member it maps to most directly, or declare it a collection. */
function renderCoreResolution(group) {
  const sec = el("section", "block core-res");
  const cstate = coreState(group);
  sec.append(blockTitle("Core code term", "the single code term this group maps to most directly"));
  const note = el("div", "core-note " + (cstate === "unresolved" ? "warn" : ""));
  note.textContent =
    cstate === "resolved"
      ? "Resolved — keep its term & definition aligned with the group's (they describe one concept)."
      : cstate === "collection"
        ? "This group is a collection of distinct parts with no single core code term."
        : "Unresolved — pick the one code term this group most directly maps to, or mark it a collection.";
  sec.append(note);
  const linked = (group.rolls_up || []).filter((m) => m.concept_id);
  const picker = el("div", "core-picker");
  for (const m of linked) {
    const c = state.data.concepts.find((x) => x.id === m.concept_id);
    const chip = el("button", "core-choice" + (m.core ? " on" : ""), (m.core ? "★ " : "") + (c ? c.canonical_term : m.term));
    chip.onclick = () => { if (m.core) { delete m.core; } else { setGroupCore(group, m.concept_id); } saveConcept(group, true); };
    picker.append(chip);
  }
  const coll = el("button", "core-choice collection" + (group.no_single_core ? " on" : ""), "no single core (collection)");
  coll.onclick = () => { markGroupCollection(group, !group.no_single_core); saveConcept(group, true); };
  picker.append(coll);
  sec.append(picker);
  return sec;
}

/* the Members section of a group: each member is editable inline (linked members
 * edit the real concept record; mngr members are marked but still editable). */
function renderGroupMembers(group) {
  const sec = el("section", "block");
  sec.append(blockTitle("Members", "the pieces this groups — expand to edit, or open full ↗"));
  const list = el("div", "members");
  (group.rolls_up || []).forEach((m, idx) => {
    const linked = m.concept_id ? state.data.concepts.find((x) => x.id === m.concept_id) : null;
    const locked = m.scope === "mngr-inherited";
    const card = el("div", "member-card" + (locked ? " locked" : ""));
    const head = el("div", "member-head");
    const caret = el("span", "mcaret", "▸");
    head.append(caret, el("span", "member-title", linked ? linked.canonical_term : (m.term || "(member)")));
    if (m.core) card.classList.add("is-core");
    if (linked && conceptDecided(linked)) head.append(el("span", "member-done", "✓"));
    // core: the single code term this group most directly maps to. Click to set/unset.
    const coreTag = el("span", "core-tag" + (m.core ? " on" : ""), m.core ? "★ core" : "☆ core");
    coreTag.title = m.core ? "This is the group's core code term. Click to unset." : "Mark as the group's core code term";
    coreTag.onclick = (e) => { e.stopPropagation(); if (m.core) { delete m.core; } else if (m.concept_id) { setGroupCore(group, m.concept_id); } else { (group.rolls_up || []).forEach((mm) => delete mm.core); m.core = true; group.no_single_core = false; } saveConcept(group, true); };
    head.append(coreTag);
    const badge = el("span", "scope-toggle " + (locked ? "mngr" : "minds"), locked ? "🔒 mngr" : "minds");
    badge.title = locked ? "Off-limits (from mngr). Click to mark in-scope." : "In scope. Click to mark off-limits (mngr).";
    badge.onclick = (e) => { e.stopPropagation(); m.scope = locked ? "minds" : "mngr-inherited"; if (linked) { linked.scope = m.scope; scheduleSave("concept", linked); } saveConcept(group, true); };
    head.append(badge);
    if (linked) {
      const open = el("span", "open-full", "open ↗");
      open.onclick = (e) => { e.stopPropagation(); selectItem("concept", linked.id); };
      head.append(open);
    }
    // remove the member from this group (a linked concept reappears as a standalone term)
    const rm = el("span", "member-rm", "×");
    rm.title = linked ? "Remove from this group (the term stays, becomes standalone again)" : "Remove this layer";
    rm.onclick = (e) => { e.stopPropagation(); group.rolls_up.splice(idx, 1); saveConcept(group, true); };
    head.append(rm);
    const body = el("div", "member-body hidden");
    if (linked) {
      body.append(renderCompactConcept(linked));
    } else {
      body.append(field("Term", textInput(m.term, (v) => { m.term = v; saveConcept(group, false); }, { mono: true })));
      body.append(field("Code term", textInput(m.code_term, (v) => { m.code_term = v; saveConcept(group, false); }, { mono: true })));
      body.append(field("Note", textInput(m.note, (v) => { m.note = v; saveConcept(group, false); }, { textarea: true })));
    }
    head.onclick = () => { const hid = body.classList.toggle("hidden"); caret.textContent = hid ? "▸" : "▾"; };
    card.append(head, body);
    // the core term and its group describe one concept — flag when their definitions drift
    if (m.core && linked && (linked.working_definition || "") !== (group.working_definition || "")) {
      const drift = el("div", "core-drift");
      drift.append(el("span", null, "Definition differs from the group's — keep them aligned:"));
      const useGroup = el("button", "add-btn", "← use group's");
      useGroup.onclick = (e) => { e.stopPropagation(); linked.working_definition = group.working_definition; scheduleSave("concept", linked); saveConcept(group, true); };
      const useTerm = el("button", "add-btn", "use term's →");
      useTerm.onclick = (e) => { e.stopPropagation(); group.working_definition = linked.working_definition; saveConcept(group, true); };
      drift.append(useGroup, useTerm);
      card.append(drift);
    }
    list.append(card);
  });
  sec.append(list);

  // add a member: either link an existing term (it moves under this group) or add a fresh inline layer
  const add = el("div", "member-add");
  const taken = memberSet();
  const picker = el("select", "t");
  const ph = el("option", null, "+ link an existing term…");
  ph.value = "";
  picker.append(ph);
  state.data.concepts
    .filter((x) => x.id !== group.id && !taken.has(x.id) && !(group.rolls_up || []).some((m) => m.concept_id === x.id))
    .sort((a, b) => (a.canonical_term || a.id).localeCompare(b.canonical_term || b.id))
    .forEach((x) => { const o = el("option", null, x.canonical_term || x.id); o.value = x.id; picker.append(o); });
  picker.onchange = () => { if (picker.value) { addMemberLink(group, picker.value); saveConcept(group, true); } };
  add.append(picker);
  const addInline = el("button", "add-btn", "+ add layer");
  addInline.onclick = () => { (group.rolls_up = group.rolls_up || []).push({ term: "", code_term: "", note: "", scope: "minds", concept_id: null }); saveConcept(group, true); };
  add.append(addInline);
  sec.append(add);
  return sec;
}

function slugify(s) {
  return (s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "") || "term";
}

function makeConcept(p) {
  return Object.assign({
    id: "", group: "uncategorized", kind: "concept", canonical_term: "", code_term: "", headline: null, user_facing: false,
    scope: "minds", status: "", decision_priority: null, candidate_terms: [], working_definition: "", technical_definition: "",
    enum_values: [], rolls_up: [], part_of: [], why_hard: [], to_resolve: [], overloads: [], divergences: [],
    code_locations: [], recommendation: "", sources: [], decision: { decided: false, term: "", definition: "", enum_values: [], notes: "" },
  }, p);
}

/* split a sense off into its own new non-overloaded concept */
function createConceptFromSense(cl, m) {
  const term = m.resolution.term || m.label || "new term";
  let id = slugify(term);
  let base = id, n = 2;
  while (state.data.concepts.some((c) => c.id === id)) id = base + "-" + n++;
  const c = makeConcept({
    id, canonical_term: term, working_definition: m.definition || "", scope: m.scope || "minds",
    status: "split from the overloaded word ‘" + (cl.word || cl.id) + "’",
    code_locations: m.code_location ? [{ path: m.code_location, line: null, note: "" }] : [],
  });
  state.data.concepts.push(c);
  m.resolution.target_id = id;
  scheduleSave("concept", c);
  saveCluster(cl, true);
  selectItem("concept", id);
}

/* every overloaded-word sense routed (merged or split) into this concept */
function routedInSenses(conceptId) {
  const out = [];
  for (const cl of state.data.clusters) {
    for (const m of cl.distinct_meanings || []) {
      if (m.resolution && m.resolution.target_id === conceptId) out.push({ cl, m });
    }
  }
  return out;
}

const RES_ACTIONS = [
  ["", "— unresolved —"],
  ["keep", "keep the word for this sense"],
  ["own", "give it its own separate term"],
  ["merge", "merge into another concept"],
];

function resolutionBadge(res) {
  if (!res || !res.action) return null;
  let text = "";
  if (res.action === "keep") text = "keeps the word" + (res.term ? ": " + res.term : "");
  else if (res.action === "own") text = "→ own term" + (res.term ? ": " + res.term : "");
  else if (res.action === "merge") {
    const t = res.target_id && state.data.concepts.find((c) => c.id === res.target_id);
    text = "→ merges into " + (t ? t.canonical_term : "…");
  }
  return el("span", "res-badge res-" + res.action, text);
}

/* the per-sense resolution editor for an overloaded word: each sense is routed
 * to its destination (keep the word / split into its own term / merge into a
 * concept), which is the actual cross-cutting resolution work. */
function renderSenseList(cl) {
  cl.distinct_meanings = cl.distinct_meanings || [];
  const wrap = el("div", "list-rows");
  const sortedConcepts = state.data.concepts.slice().sort((a, b) => (a.canonical_term || "").localeCompare(b.canonical_term || ""));
  cl.distinct_meanings.forEach((m, idx) => {
    m.resolution = m.resolution || { action: "", term: "", target_id: "", note: "" };
    const locked = m.scope === "mngr-inherited";
    const resolved = !!m.resolution.action;
    const card = el("div", "member-card sense" + (locked ? " locked" : "") + (resolved ? " resolved" : ""));
    const head = el("div", "member-head" + (resolved ? "" : " static"));
    const caret = resolved ? el("span", "mcaret", "▸") : null;
    if (caret) head.append(caret);
    head.append(el("span", "member-title", m.label || "(sense)"));
    const rb = resolutionBadge(m.resolution);
    if (rb) head.append(rb);
    const badge = el("span", "scope-toggle " + (locked ? "mngr" : "minds"), locked ? "🔒 mngr" : "minds");
    badge.title = locked ? "Off-limits (from mngr). Click to mark in-scope." : "In scope. Click to mark off-limits (mngr).";
    badge.onclick = (e) => { e.stopPropagation(); m.scope = locked ? "minds" : "mngr-inherited"; saveCluster(cl, true); };
    head.append(badge);
    const x = el("button", "x-btn", "×");
    x.onclick = (e) => { e.stopPropagation(); cl.distinct_meanings.splice(idx, 1); saveCluster(cl, true); };
    head.append(x);
    card.append(head);

    // resolved senses collapse their detail (they've stopped being their own thing here)
    const body = el("div", "sense-body" + (resolved ? " hidden" : ""));
    body.append(field("Sense", textInput(m.label, (v) => { m.label = v; saveCluster(cl, false); })));
    body.append(field("Definition", textInput(m.definition, (v) => { m.definition = v; saveCluster(cl, false); }, { textarea: true })));
    body.append(field("Code location", textInput(m.code_location, (v) => { m.code_location = v; saveCluster(cl, false); }, { mono: true })));
    card.append(body);
    if (caret) head.onclick = () => { const hid = body.classList.toggle("hidden"); caret.textContent = hid ? "▸" : "▾"; };

    const res = el("div", "resolution");
    res.append(el("div", "res-title", "↳ Resolution — where this sense goes"));
    const sel = el("select", "t");
    for (const [val, lab] of RES_ACTIONS) {
      const o = el("option", null, lab);
      o.value = val;
      if (val === m.resolution.action) o.selected = true;
      sel.append(o);
    }
    sel.onchange = () => { m.resolution.action = sel.value; saveCluster(cl, true); };
    res.append(field("Action", sel));
    if (m.resolution.action === "keep" || m.resolution.action === "own") {
      res.append(field(m.resolution.action === "own" ? "Its own term" : "The word it keeps", textInput(m.resolution.term, (v) => { m.resolution.term = v; saveCluster(cl, false); }, { mono: true, placeholder: "canonical term for this sense" })));
    }
    if (m.resolution.action === "own") {
      const linked = m.resolution.target_id && state.data.concepts.find((c) => c.id === m.resolution.target_id);
      const slot = el("div", "res-action");
      if (linked) {
        const j = el("span", "open-full", "✓ created — open ‘" + linked.canonical_term + "’ ↗");
        j.onclick = () => selectItem("concept", linked.id);
        slot.append(j);
      } else {
        const mk = el("button", "add-btn", "↳ create as a new concept");
        mk.onclick = () => createConceptFromSense(cl, m);
        slot.append(mk);
      }
      res.append(slot);
    }
    if (m.resolution.action === "merge") {
      const tsel = el("select", "t");
      const opt0 = el("option", null, "— pick a concept —");
      opt0.value = "";
      tsel.append(opt0);
      for (const c of sortedConcepts) {
        const o = el("option", null, (c.kind === "group" ? "❖ " : "") + (c.canonical_term || c.id));
        o.value = c.id;
        if (c.id === m.resolution.target_id) o.selected = true;
        tsel.append(o);
      }
      tsel.onchange = () => { m.resolution.target_id = tsel.value; saveCluster(cl, true); };
      const f = field("Merge into", tsel);
      if (m.resolution.target_id) {
        const j = el("span", "open-full", "open ↗");
        j.onclick = () => selectItem("concept", m.resolution.target_id);
        f.append(j);
      }
      res.append(f);
    }
    res.append(field("Note", textInput(m.resolution.note, (v) => { m.resolution.note = v; saveCluster(cl, false); }, { placeholder: "why" })));
    card.append(res);
    wrap.append(card);
  });
  const add = el("button", "add-btn", "+ add sense");
  add.onclick = () => { cl.distinct_meanings.push({ label: "", definition: "", code_location: "", scope: "minds", resolution: { action: "", term: "", target_id: "", note: "" } }); saveCluster(cl, true); };
  const container = el("div");
  container.append(wrap, add);
  return container;
}

/* ------------------------------------------------------------------ editor pane */
function renderEditor() {
  const pane = document.getElementById("editorPane");
  pane.innerHTML = "";
  const wrap = el("div", "editor-wrap reveal");
  pane.append(wrap);
  if (!state.selected) {
    wrap.append(el("div", "empty", "Select a concept to begin."));
    return;
  }
  if (state.selected.kind === "concept") {
    const c = state.data.concepts.find((x) => x.id === state.selected.id);
    renderConceptEditor(wrap, c);
  } else {
    const cl = state.data.clusters.find((x) => x.id === state.selected.id);
    renderClusterEditor(wrap, cl);
  }
}

function saveConcept(c, restructure) {
  scheduleSave("concept", c);
  renderCounts();
  renderIndex();
  if (restructure) renderEditor();
}

function renderConceptEditor(wrap, c) {
  const decided = conceptDecided(c);
  // header
  const head = el("div", "concept-head");
  head.append(el("h1", "concept-name", c.canonical_term || c.id));
  const chips = el("div", "meta-chips");
  const grp = state.data.groups.find((g) => g.id === c.group);
  chips.append(el("span", "mchip", (grp ? grp.title : c.group)));
  chips.append(el("span", "mchip scope-" + (c.scope === "mngr-inherited" ? "mngr" : "minds"), c.scope || "minds"));
  chips.append(el("span", "mchip", c.user_facing ? "user-facing" : "internal"));
  if (c.code_term) chips.append(el("span", "mchip code", "⌘ " + c.code_term));
  if (c.status) chips.append(el("span", "mchip" + (/contest|needs|warn/.test(c.status) ? " status-warn" : ""), c.status));
  if (c.kind === "group") {
    const cs = coreState(c);
    const label = cs === "resolved" ? "★ core: " + (groupCoreMember(c).term || "set") : cs === "collection" ? "collection" : "core unresolved";
    chips.append(el("span", "mchip " + (cs === "unresolved" ? "status-warn" : "scope-minds"), label));
  }
  head.append(chips);
  wrap.append(head);

  if (c.scope === "mngr-inherited") {
    const b = el("div", "scope-banner");
    b.innerHTML = "<span>✶</span><span><b>Inherited from mngr.</b> The code term comes from elsewhere in mngr and is off-limits to rename — but the user-facing framing is still yours to canonize.</span>";
    wrap.append(b);
    // Advisory: an infra (mngr) term that is shown to users is usually a smell —
    // either users shouldn't see infra naming, or this deserves its own canonical term.
    if (c.user_facing) {
      const s = el("div", "scope-banner suggest");
      s.innerHTML = "<span>⚠</span><span><b>Shown to users, but this is mngr infrastructure naming.</b> Consider whether users should see an infra term here — or whether the user-facing concept deserves its own canonical term (or shouldn't be user-facing at all).</span>";
      wrap.append(s);
    }
  }

  /* THE CANONICAL ENTRY — the single editable block (term, definitions, values, decided) */
  c.decision = c.decision || { decided: false, notes: "" };
  const del = !!c.to_delete;
  const dec = el("div", "decision" + (decided ? " is-decided" : "") + (del ? " is-delete" : ""));
  dec.append(el("span", "decision-tab", del ? "✗ To delete" : decided ? "✓ Decided" : "To decide"));

  // facing decision first — it determines whether a user-facing name even applies
  dec.append(toggle(c.user_facing, c.user_facing ? "shown to users" : "internal only", (v) => { c.user_facing = v; saveConcept(c, true); }));

  // name(s): user-facing name (when shown) + canonical term, related per nameFields' rule
  for (const f of nameFields(c, (r) => saveConcept(c, r))) dec.append(f);

  // editable candidate terms (adopt one in place — single home for candidates)
  dec.append(field("Candidate terms", stringList(c.candidate_terms || (c.candidate_terms = []), (r) => saveConcept(c, r), { mono: true, placeholder: "a candidate name", adopt: (v) => { if (v) { c.canonical_term = v; saveConcept(c, true); } } }), "↑ adopt one as the canonical term"));

  // definitions: plain-language and technical are genuinely different facets
  dec.append(field("Definition (plain language)", textInput(c.working_definition, (v) => { c.working_definition = v; saveConcept(c, false); }, { textarea: true, placeholder: "how you'd define it for a user" })));
  dec.append(field("Definition (technical)", textInput(c.technical_definition, (v) => { c.technical_definition = v; saveConcept(c, false); }, { textarea: true, placeholder: "the precise engineering meaning" })));

  // values / enum
  c.enum_values = c.enum_values || [];
  dec.append(field("Values", objectList(c.enum_values, [ { key: "value", label: "value", mono: true, placeholder: "VALUE" }, { key: "note", label: "meaning", placeholder: "what it means" } ], (r) => saveConcept(c, r), "cols-2 enum-rows"), "optional — the canonical set of possible values"));

  // decided + notes (hidden when the term is marked for deletion)
  if (!del) dec.append(toggle(c.decision.decided, c.decision.decided ? "Decided — locked in" : "Mark as decided", (v) => { c.decision.decided = v; if (v) c.to_delete = false; saveConcept(c, true); }));
  dec.append(field("Notes", textInput(c.decision.notes, (v) => { c.decision.notes = v; saveConcept(c, false); }, { textarea: true, placeholder: "reasoning, open questions, anything…" })));
  // to-delete disposition: this term shouldn't be a concept at all
  const dz = el("div", "delete-zone");
  dz.append(toggle(c.to_delete, c.to_delete ? "Marked to delete — not a real concept" : "Mark to delete", (v) => { c.to_delete = v; if (v) c.decision.decided = false; saveConcept(c, true); }));
  if (c.to_delete) dz.append(field("Why delete", textInput(c.delete_reason, (v) => { c.delete_reason = v; saveConcept(c, false); }, { textarea: true, placeholder: "why this shouldn't be a term we track / canonize" })));
  dec.append(dz);
  wrap.append(dec);

  /* DECIDING FACTORS — surfaced right under the decision, since they drive it */
  if ((c.why_hard || []).length || (c.to_resolve || []).length || c.recommendation || !c.decision.decided) {
    const sFactors = el("section", "block factors");
    sFactors.append(blockTitle("Why it's hard", "the ambiguities behind this decision"));
    sFactors.append(stringList(c.why_hard || (c.why_hard = []), (r) => saveConcept(c, r), { textarea: true }));
    sFactors.append(blockTitle("To resolve", "the decisions that would settle it"));
    sFactors.append(stringList(c.to_resolve || (c.to_resolve = []), (r) => saveConcept(c, r), { textarea: true }));
    sFactors.append(field("Recommendation", textInput(c.recommendation, (v) => { c.recommendation = v; saveConcept(c, false); }, { textarea: true }), "the analysis's suggested resolution"));
    wrap.append(sFactors);
  }

  if (c.kind === "group") {
    const lockedMembers = (c.rolls_up || []).filter((m) => m.scope === "mngr-inherited").length;
    const note = el("div", "group-note");
    note.innerHTML = "<span>❖</span><span>This is a <b>group</b>. Set its own canonical term &amp; definition above; its members are editable below" + (lockedMembers ? " — " + lockedMembers + " come from mngr and are marked off-limits, but you can still edit them" : "") + ".</span>";
    wrap.append(note);
    wrap.append(renderCoreResolution(c));
    wrap.append(renderGroupMembers(c));
  }

  /* senses routed in from overloaded words (the re-parenting side of a merge) */
  const routed = routedInSenses(c.id);
  if (routed.length) {
    const rs = el("section", "block");
    rs.append(blockTitle("Routed-in senses", routed.length + " sense" + (routed.length > 1 ? "s" : "") + " merged here from overloaded words"));
    for (const { cl, m } of routed) {
      const card = el("div", "member-card routed");
      const head = el("div", "member-head static");
      head.append(el("span", "member-title", m.label || "(sense)"));
      head.append(el("span", "res-badge res-merge", "from ‘" + (cl.word || cl.id) + "’"));
      const open = el("span", "open-full", "open word ↗");
      open.onclick = () => selectItem("cluster", cl.id);
      head.append(open);
      card.append(head);
      const body = el("div", "sense-body");
      body.append(field("Definition", textInput(m.definition, (v) => { m.definition = v; saveCluster(cl, false); }, { textarea: true })));
      card.append(body);
      rs.append(card);
    }
    wrap.append(rs);
  }

  /* CLASSIFICATION */
  const s2 = el("section", "block");
  s2.append(blockTitle("Classification"));
  s2.append(field("Kind", selectInput(c.kind || "concept", ["concept", "group"], (v) => { c.kind = v; if (v === "group") c.rolls_up = c.rolls_up || []; saveConcept(c, true); }), "a 'group' nests other terms as its layers / parts"));
  s2.append(field("Category", selectInput(c.group, state.data.groups.map((g) => g.id), (v) => { c.group = v; saveConcept(c, true); })));
  s2.append(field("Scope", selectInput(c.scope || "minds", ["minds", "mngr-inherited"], (v) => { c.scope = v; saveConcept(c, true); })));
  s2.append(field("Status", textInput(c.status, (v) => { c.status = v; saveConcept(c, false); }, { placeholder: "settled / contested / needs-rename…" })));
  wrap.append(s2);

  /* RELATIONSHIPS */
  const s6 = el("section", "block");
  s6.append(blockTitle("Relationships"));
  s6.append(field("Part of", stringList(c.part_of || (c.part_of = []), (r) => saveConcept(c, r), { mono: true, jumpKind: "concept", placeholder: "concept id" })));
  s6.append(field("Overloaded words it's tangled in", stringList(c.overloads || (c.overloads = []), (r) => saveConcept(c, r), { mono: true, jumpKind: "cluster", placeholder: "cluster id" })));
  wrap.append(s6);

  /* DIVERGENCES */
  const s7 = el("section", "block");
  s7.append(blockTitle("Doc / code divergences"));
  s7.append(objectList(c.divergences || (c.divergences = []), [ { key: "ref", label: "ref #", mono: true }, { key: "severity", label: "severity", options: ["HIGH", "MED", "LOW"] }, { key: "summary", label: "summary", full: true, textarea: true }, { key: "citation", label: "citation", mono: true, full: true } ], (r) => saveConcept(c, r), "cols-3"));
  wrap.append(s7);

  /* CODE LOCATIONS */
  const s8 = el("section", "block");
  s8.append(blockTitle("Code locations"));
  s8.append(objectList(c.code_locations || (c.code_locations = []), [ { key: "path", label: "path", mono: true, full: true }, { key: "line", label: "line", mono: true }, { key: "note", label: "note" } ], (r) => saveConcept(c, r), "cols-3"));
  wrap.append(s8);

  /* SOURCES */
  const s9 = el("section", "block");
  s9.append(blockTitle("Source excerpts", "from your vault — open ▦ Source for the full docs"));
  s9.append(objectList(c.sources || (c.sources = []), [ { key: "doc", label: "doc", mono: true }, { key: "anchor", label: "anchor", full: true }, { key: "excerpt", label: "excerpt", full: true, textarea: true } ], (r) => saveConcept(c, r), "cols-2"));
  wrap.append(s9);
}

function saveCluster(cl, restructure) {
  scheduleSave("cluster", cl);
  renderCounts();
  renderIndex();
  if (restructure) renderEditor();
}

function renderClusterEditor(wrap, cl) {
  const decided = clusterDecided(cl);
  cl.decision = cl.decision || { decided: false, notes: "" };
  const head = el("div", "concept-head");
  head.append(el("h1", "concept-name", cl.word || cl.id));
  const senses = cl.distinct_meanings || [];
  const resolvedCount = senses.filter((m) => m.resolution && m.resolution.action).length;
  const chips = el("div", "meta-chips");
  chips.append(el("span", "mchip", "overloaded word"));
  if (cl.blast_radius_rank != null) chips.append(el("span", "mchip", "blast radius #" + cl.blast_radius_rank));
  chips.append(el("span", "mchip" + (resolvedCount === senses.length ? " scope-minds" : " status-warn"), resolvedCount + "/" + senses.length + " senses routed"));
  head.append(chips);
  wrap.append(head);

  const note = el("div", "group-note");
  note.innerHTML = "<span>❖</span><span>This is an <b>overloaded word</b> — not a concept to name, but several unrelated senses to <b>route</b>. For each sense below: keep the word, split it into its own new term, or merge it into another concept. The word itself dissolves once every sense is routed.</span>";
  wrap.append(note);

  const cdel = !!cl.to_delete;
  const bar = el("div", "decision" + (decided ? " is-decided" : "") + (cdel ? " is-delete" : "") + " slim");
  bar.append(el("span", "decision-tab", cdel ? "✗ To delete" : decided ? "✓ Resolved" : "Routing"));
  if (!cdel) bar.append(toggle(cl.decision.decided, cl.decision.decided ? "Resolved — all senses routed" : "Mark resolved", (v) => { cl.decision.decided = v; if (v) cl.to_delete = false; saveCluster(cl, true); }));
  bar.append(field("Notes", textInput(cl.decision.notes, (v) => { cl.decision.notes = v; saveCluster(cl, false); }, { textarea: true, placeholder: "anything about resolving this word…" })));
  const cdz = el("div", "delete-zone");
  cdz.append(toggle(cl.to_delete, cl.to_delete ? "Marked to delete — not a real concept" : "Mark to delete", (v) => { cl.to_delete = v; if (v) cl.decision.decided = false; saveCluster(cl, true); }));
  if (cl.to_delete) cdz.append(field("Why delete", textInput(cl.delete_reason, (v) => { cl.delete_reason = v; saveCluster(cl, false); }, { textarea: true, placeholder: "why this shouldn't be a term we track / canonize" })));
  bar.append(cdz);
  wrap.append(bar);

  const s1 = el("section", "block");
  s1.append(blockTitle("Senses — resolve each", "🔒 = off-limits (mngr), still routable"));
  s1.append(renderSenseList(cl));
  wrap.append(s1);

  const s2 = el("section", "block");
  s2.append(blockTitle("Recommended disambiguation", "from the analysis"));
  s2.append(field("", textInput(cl.recommended_decision, (v) => { cl.recommended_decision = v; saveCluster(cl, false); }, { textarea: true })));
  wrap.append(s2);

  const s0 = el("section", "block");
  s0.append(blockTitle("Details"));
  s0.append(field("Word", textInput(cl.word, (v) => { cl.word = v; saveCluster(cl, false); }, { mono: true })));
  s0.append(field("Title", textInput(cl.title, (v) => { cl.title = v; saveCluster(cl, false); })));
  s0.append(field("Blast radius rank", textInput(cl.blast_radius_rank, (v) => { cl.blast_radius_rank = v; saveCluster(cl, false); }, { mono: true })));
  s0.append(field("Scope", selectInput(cl.scope || "minds", ["minds", "mngr-inherited"], (v) => { cl.scope = v; saveCluster(cl, true); })));
  wrap.append(s0);

  const s3 = el("section", "block");
  s3.append(blockTitle("Source excerpts"));
  s3.append(objectList(cl.sources || (cl.sources = []), [ { key: "doc", label: "doc", mono: true }, { key: "anchor", label: "anchor", full: true }, { key: "excerpt", label: "excerpt", full: true, textarea: true } ], (r) => saveCluster(cl, r), "cols-2"));
  wrap.append(s3);
}

/* ------------------------------------------------------------------ source pane */
function renderSource() {
  const body = document.getElementById("sourceBody");
  body.innerHTML = "";
  if (!state.selected) return;
  const item = state.selected.kind === "concept"
    ? state.data.concepts.find((x) => x.id === state.selected.id)
    : state.data.clusters.find((x) => x.id === state.selected.id);
  if (!item) return;

  for (const src of item.sources || []) {
    const card = el("div", "src-card reveal");
    const docLine = el("div", "src-doc");
    const link = el("a", null, "▤ " + src.doc);
    link.onclick = () => openDoc(src.doc);
    docLine.append(link);
    card.append(docLine);
    if (src.anchor) card.append(el("div", "src-anchor", src.anchor));
    if (src.excerpt) card.append(el("div", "src-excerpt", "“" + src.excerpt + "”"));
    body.append(card);
  }

  const locs = item.code_locations || [];
  if (locs.length) {
    body.append(el("div", "block-title", "Code"));
    const cl = el("div", "code-list");
    for (const loc of locs) {
      const r = el("div", "code-loc");
      r.append(document.createTextNode(loc.path || ""));
      if (loc.line) r.append(el("span", "ln", ":" + loc.line));
      if (loc.note) r.append(el("span", "note", "— " + loc.note));
      cl.append(r);
    }
    body.append(cl);
  }
}

async function openDoc(name) {
  setDrawer(true);
  const docName = name;
  const body = document.getElementById("sourceBody");
  body.innerHTML = "";
  const back = el("span", "back", "← back to excerpts");
  back.onclick = () => renderSource();
  body.append(back);
  const viewer = el("div", "doc-viewer reveal");
  body.append(viewer);
  try {
    const r = await fetch("api/docs/" + encodeURIComponent(docName));
    if (!r.ok) throw new Error("not found");
    viewer.innerHTML = await r.text();
  } catch (e) {
    viewer.append(el("div", "empty", "Couldn't load " + docName));
  }
}

boot();
