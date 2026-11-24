// The Disk Space page: the gauge, the treemap, the three lists, the settings and the history,
// all read from /api/state. Deleting goes through /api/delete after a confirmation sheet; the
// cleanup helper is started by /api/helper.
"use strict";

const GB = 1024 ** 3;
const MB = 1024 ** 2;
const POLL_MS = 15000;
const SCAN_POLL_MS = 3000;

let state = null;
let byId = new Map();
let childrenOf = new Map();
let path = [];

const $ = (id) => document.getElementById(id);
function fmt(b) {
  if (b <= 0) return "0 MB";
  if (b >= GB) return `${(b / GB).toFixed(1)} GB`;
  if (b < MB) return "under 1 MB";
  return `${Math.round(b / MB)} MB`;
}

function ago(epoch) {
  if (!epoch) return "never";
  const s = Math.max(0, Date.now() / 1000 - epoch);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} minute${Math.round(s / 60) === 1 ? "" : "s"} ago`;
  if (s < 86400) return `${Math.round(s / 3600)} hour${Math.round(s / 3600) === 1 ? "" : "s"} ago`;
  return new Date(epoch * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 5000);
}

async function api(method, url, body) {
  const response = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `The app answered ${response.status}.`);
  return data;
}

// Gauge and alert.
function renderGauge() {
  const { disk, threshold } = state;
  $("used-text").innerHTML = `${disk.percent}% full <small>${fmt(disk.used)} of ${fmt(disk.total)}</small>`;
  $("gauge-fill").style.width = `${disk.percent}%`;
  $("gauge").classList.toggle("full", disk.percent >= threshold);
  $("gauge-mark").style.left = `${threshold}%`;
  $("free-text").textContent = `${fmt(disk.free)} free`;
  $("alert-line-text").textContent = `Alert at ${threshold}%`;
  $("alert").hidden = disk.percent < threshold;
  if (document.activeElement !== $("threshold")) $("threshold").value = threshold;
  const checked = state.last_check_at ? `Checked ${ago(state.last_check_at)}.` : "Not checked yet.";
  $("last-check").textContent = `${checked} The disk is checked every 10 minutes; past the line, the cleanup helper opens and you get a notification.`;
}

function renderSnapshotAge() {
  const el = $("snapshot-age");
  el.innerHTML = "";
  if (state.scanning) {
    el.textContent = "Measuring the disk... (about a minute)";
    return;
  }
  const snap = state.snapshot;
  if (!snap) {
    el.textContent = "Not measured yet";
    return;
  }
  const link = document.createElement("a");
  link.href = `/raw/snapshot/${snap.id}`;
  link.target = "_blank";
  link.textContent = `Measured ${ago(snap.taken_at)}`;
  el.appendChild(link);
}

// Squarified treemap.
function squarify(items, x, y, w, h, out) {
  if (!items.length || w <= 0 || h <= 0) return;
  const total = items.reduce((s, i) => s + i.v, 0);
  if (total <= 0) return;
  const short = Math.min(w, h);
  const scale = (w * h) / total;
  let row = [];
  let best = Infinity;
  let i = 0;
  for (; i < items.length; i++) {
    const trial = [...row, items[i]];
    const s = trial.reduce((a, b) => a + b.v * scale, 0);
    const worst = Math.max(...trial.map((t) => Math.max((short * short * t.v * scale) / (s * s), (s * s) / (short * short * t.v * scale))));
    if (worst > best) break;
    best = worst;
    row = trial;
  }
  const thick = row.reduce((a, b) => a + b.v * scale, 0) / short;
  let offset = 0;
  for (const r of row) {
    const len = (r.v * scale) / thick;
    if (w >= h) out.push({ item: r, x, y: y + offset, w: thick, h: len });
    else out.push({ item: r, x: x + offset, y, w: len, h: thick });
    offset += len;
  }
  if (w >= h) squarify(items.slice(i), x + thick, y, w - thick, h, out);
  else squarify(items.slice(i), x, y + thick, w, h - thick, out);
}

function indexNodes() {
  byId = new Map();
  childrenOf = new Map();
  for (const node of state.snapshot ? state.snapshot.nodes : []) {
    byId.set(node.id, node);
    if (!childrenOf.has(node.parent)) childrenOf.set(node.parent, []);
    childrenOf.get(node.parent).push(node);
  }
  // Keep the drill-down where it was if those folders are still there.
  path = path.map((n) => byId.get(n.id)).filter(Boolean);
  const root = (childrenOf.get(null) || [])[0];
  if (root && (!path.length || path[0].id !== root.id)) path = [root];
}

function describe(node) {
  let text = `${node.name}: ${fmt(node.bytes)}`;
  if (childrenOf.has(node.id)) {
    text += node.safe_bytes ? `, ${fmt(node.safe_bytes)} of it safe to delete.` : ".";
    text += " Click to look inside.";
  } else if (node.why) text += `. ${node.why}`;
  if (node.path) text += `  (${node.path})`;
  return text;
}

function renderTreemap() {
  const box = $("treemap");
  box.innerHTML = "";
  const crumbs = $("crumbs");
  crumbs.innerHTML = "";
  if (!path.length) {
    box.innerHTML = `<div class="treemap-empty">${state.scanning ? "Measuring the disk..." : "Press \u201cMeasure again\u201d to see what is using the disk."}</div>`;
    return;
  }
  const node = path[path.length - 1];
  const W = box.clientWidth;
  const H = box.clientHeight;
  const items = (childrenOf.get(node.id) || []).map((c) => ({ node: c, v: c.bytes })).filter((i) => i.v > 0).sort((a, b) => b.v - a.v);
  const rects = [];
  squarify(items, 0, 0, W, H, rects);
  for (const r of rects) {
    const n = r.item.node;
    const opens = childrenOf.has(n.id);
    const el = document.createElement("div");
    el.className = `tile ${n.verdict}` + (opens ? " can-open" : "");
    Object.assign(el.style, { left: `${r.x}px`, top: `${r.y}px`, width: `${r.w}px`, height: `${r.h}px` });
    el.dataset.node = n.id;
    if (n.path) el.dataset.path = n.path;
    if (r.w > 50 && r.h > 34) {
      el.innerHTML = `<div class="name"></div><div class="size num">${fmt(n.bytes)}</div>`;
      el.querySelector(".name").textContent = opens ? `${n.name} \u203a` : n.name;
    }
    el.title = describe(n);
    el.addEventListener("mouseenter", () => { $("hover-info").textContent = describe(n); });
    if (opens) el.addEventListener("click", () => { path.push(n); renderTreemap(); });
    box.appendChild(el);
  }
  path.forEach((p, idx) => {
    if (idx) crumbs.insertAdjacentHTML("beforeend", `<span class="sep">/</span>`);
    const a = document.createElement(idx === path.length - 1 ? "span" : "a");
    a.textContent = `${p.name} (${fmt(p.bytes)})`;
    if (idx < path.length - 1) a.addEventListener("click", () => { path = path.slice(0, idx + 1); renderTreemap(); });
    crumbs.appendChild(a);
  });
}

// The three lists: every item (a folder a rule decided) of the latest measurement.
const selected = new Set();

function renderLists() {
  const all = state.snapshot ? state.snapshot.nodes.filter((n) => n.item && n.bytes > 0) : [];
  all.sort((a, b) => b.bytes - a.bytes);
  for (const verdict of ["safe", "ask", "keep"]) {
    const list = $(`${verdict}-list`);
    const mine = all.filter((l) => l.verdict === verdict);
    list.className = "items" + (verdict === "safe" ? "" : " no-check");
    list.innerHTML = "";
    $(`${verdict}-total`).textContent = fmt(mine.reduce((s, l) => s + l.bytes, 0));
    if (!mine.length) list.innerHTML = `<li class="secondary">Nothing here right now.</li>`;
    for (const l of mine) {
      const li = document.createElement("li");
      li.dataset.path = l.path;
      if (verdict === "safe") {
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.disabled = !l.deletable;
        cb.checked = selected.has(l.path);
        cb.addEventListener("change", () => {
          if (cb.checked) selected.add(l.path); else selected.delete(l.path);
          updateDeleteButton();
        });
        li.appendChild(cb);
      }
      li.insertAdjacentHTML("beforeend", `<div class="what"><div class="n"></div><div class="why"></div></div><span class="where"></span><span class="sz">${fmt(l.bytes)}</span>`);
      li.querySelector(".n").textContent = l.name;
      li.querySelector(".why").textContent = l.why;
      li.querySelector(".where").textContent = l.path;
      li.querySelector(".where").title = l.path;
      if (verdict === "ask") {
        const b = document.createElement("button");
        b.className = "link ask-action";
        b.type = "button";
        b.textContent = "Ask about this";
        b.addEventListener("click", () => startHelper(l.path));
        li.appendChild(b);
      }
      list.appendChild(li);
    }
  }
  for (const p of [...selected]) if (!all.some((l) => l.path === p && l.deletable)) selected.delete(p);
  updateDeleteButton();
}

function selectedItems() {
  return state.snapshot ? state.snapshot.nodes.filter((n) => n.item && selected.has(n.path)) : [];
}

function updateDeleteButton() {
  const sel = selectedItems();
  const b = $("delete-selected");
  b.disabled = !state.is_owner || !sel.length;
  b.textContent = sel.length ? `Delete selected (${fmt(sel.reduce((s, l) => s + l.bytes, 0))})` : "Delete selected";
}

function renderHistory() {
  const list = $("history");
  list.innerHTML = "";
  if (!state.history.length) {
    list.innerHTML = `<li class="secondary">Nothing yet. Alerts, deletions and cleanup helper chats show up here.</li>`;
    return;
  }
  for (const h of state.history) {
    const li = document.createElement("li");
    const text = document.createElement("span");
    const freed = h.freed_bytes ? ` Freed ${fmt(h.freed_bytes)}.` : "";
    text.textContent = `${ago(h.at)} \u00b7 ${h.summary}${freed}`;
    const link = document.createElement("a");
    link.href = `/raw/history/${h.id}`;
    link.target = "_blank";
    link.textContent = "Details";
    li.append(text, link);
    list.appendChild(li);
  }
}

function renderAll() {
  renderGauge();
  renderSnapshotAge();
  indexNodes();
  renderTreemap();
  renderLists();
  renderHistory();
  renderOwnerControls();
}

let pollTimer = null;

// A visitor who was given this app can look through the breakdown but cannot free space: the server refuses those
// calls, and greying out the controls that would make them keeps the page from offering what will not happen.
function renderOwnerControls() {
  $("owner-note").hidden = state.is_owner;
  for (const id of ["select-all", "alert-helper", "helper-inline"]) $(id).disabled = !state.is_owner;
  $("threshold").disabled = !state.is_owner;
  for (const button of document.querySelectorAll("#ask-list button")) button.disabled = !state.is_owner;
  updateDeleteButton();
}
async function refresh() {
  clearTimeout(pollTimer);
  try {
    const wasScanning = state && state.scanning;
    state = await api("GET", "/api/state");
    $("offline").hidden = true;
    renderAll();
    if (wasScanning && !state.scanning) toast("Measured the disk again.");
  } catch (error) {
    $("offline").hidden = false;
  }
  pollTimer = setTimeout(refresh, state && state.scanning ? SCAN_POLL_MS : POLL_MS);
}

async function startHelper(item) {
  toast("Opening the cleanup helper...");
  try {
    const result = await api("POST", "/api/helper", item ? { item } : {});
    if (!result.chat_id) toast(`The cleanup helper could not start: ${result.detail}`);
    else toast(result.created ? "The cleanup helper is opening beside this window." : "Sent that to the cleanup helper's chat.");
  } catch (error) {
    toast(`The cleanup helper could not start: ${error.message}`);
  }
  refresh();
}

// Wiring.
$("select-all").addEventListener("click", () => {
  for (const n of state.snapshot ? state.snapshot.nodes : []) if (n.item && n.deletable && n.verdict === "safe") selected.add(n.path);
  renderLists();
});
$("delete-selected").addEventListener("click", () => {
  const sel = selectedItems();
  $("sheet-list").innerHTML = "";
  for (const l of sel) {
    const li = document.createElement("li");
    li.innerHTML = `<span></span><span class="sz">${fmt(l.bytes)}</span>`;
    li.firstChild.textContent = l.name;
    $("sheet-list").appendChild(li);
  }
  $("sheet-note").textContent = `About ${fmt(sel.reduce((s, l) => s + l.bytes, 0))}. These download again or rebuild themselves when needed, or are leftovers nothing uses.`;
  $("sheet").hidden = false;
});
$("sheet-cancel").addEventListener("click", () => { $("sheet").hidden = true; });
$("sheet-confirm").addEventListener("click", async () => {
  const paths = selectedItems().map((l) => l.path);
  $("sheet-confirm").disabled = true;
  $("sheet-confirm").textContent = "Deleting...";
  try {
    const result = await api("POST", "/api/delete", { paths });
    selected.clear();
    const refused = result.refused.length ? ` ${result.refused.length} left alone: ${result.refused[0].reason}` : "";
    toast(`Freed ${fmt(result.freed_bytes)}.${refused}`);
  } catch (error) {
    toast(`Nothing was deleted: ${error.message}`);
  }
  $("sheet-confirm").disabled = false;
  $("sheet-confirm").textContent = "Delete";
  $("sheet").hidden = true;
  refresh();
});
$("alert-helper").addEventListener("click", () => startHelper(null));
$("helper-inline").addEventListener("click", () => startHelper(null));
$("rescan").addEventListener("click", async () => {
  try {
    const result = await api("POST", "/api/scan");
    if (!result.started) toast("A measurement is already running.");
  } catch (error) {
    toast(`Could not start measuring: ${error.message}`);
  }
  refresh();
});
$("threshold").addEventListener("change", async (e) => {
  try {
    const result = await api("POST", "/api/settings", { threshold: Number(e.target.value) || 90 });
    e.target.value = result.threshold;
    toast(`The alert line is now ${result.threshold}%.`);
  } catch (error) {
    toast(`Could not save that: ${error.message}`);
  }
  refresh();
});
window.addEventListener("resize", () => { if (state) renderTreemap(); });
refresh();
