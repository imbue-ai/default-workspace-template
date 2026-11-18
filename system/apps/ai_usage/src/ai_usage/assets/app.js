// Renders each AI account's usage and each chat's share of it from /api/usage.
// One section per (kind of AI, account); the view state (chosen chat, collapsed state) is kept per section.
const views = {};
const collapsed = new Set();
// Entries under "What used it" whose verbatim message the user opened; they stay open across refreshes.
const expandedTurns = new Set();
let lastData = null;
const $ = (id) => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const child of children) if (child) node.append(child);
  return node;
}

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

function fmtPct(v) {
  if (v === 0) return "0%";
  if (v < 0.1) return "<0.1%";
  return (v < 10 ? v.toFixed(1).replace(/\.0$/, "") : Math.round(v)) + "%";
}

function ago(iso) {
  const s = Math.round((Date.now() - new Date(iso)) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} hr ago`;
  return new Date(iso).toLocaleString();
}

function resetText(iso) {
  if (!iso) return "";
  const d = new Date(iso), ms = d - Date.now();
  if (ms <= 0) return "Resetting now";
  if (ms < 86400000) {
    const h = Math.floor(ms / 3600000), m = Math.floor((ms % 3600000) / 60000);
    return `Resets in ${h ? h + " hr " : ""}${m} min`;
  }
  return "Resets " + d.toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" });
}

function when(iso) {
  const d = new Date(iso), today = new Date();
  const time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  if (d.toDateString() === today.toDateString()) return `Today ${time}`;
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" }) + " " + time;
}

// One entry's description: the AI-written summary, which opens to show the verbatim message.
function turnLabel(turn, idKey) {
  if (!turn.prompt || turn.prompt === turn.label) return el("span", { class: "label", title: turn.label, text: turn.label });
  const node = el("details", { class: "label turn-detail" },
    el("summary", { title: "Show the original message", text: turn.label }),
    el("div", { class: "verbatim" },
      el("small", { text: turn.summarised ? "Original message (the line above is an AI summary of it)" : "Full message" }),
      el("p", { text: turn.prompt })));
  node.open = expandedTurns.has(idKey);
  node.addEventListener("toggle", () => {
    if (node.open) expandedTurns.add(idKey); else expandedTurns.delete(idKey);
  });
  return node;
}

function makeCollapsible(tag, idKey, attrs = {}, ...children) {
  const node = el(tag, attrs, ...children);
  if (!collapsed.has(idKey)) node.open = true;
  node.addEventListener("toggle", () => {
    if (node.open) collapsed.delete(idKey); else collapsed.add(idKey);
  });
  return node;
}

function totalCreditedForLimit(source, limitKey) {
  return source.chats.reduce((sum, c) => sum + (c.totals[limitKey] || 0), 0);
}

// Says how a limit's total splits: the part credited to chats in Studio, and the rest
// ("elsewhere"), which was used before the tracker's first reading or by something other than these chats.
function splitText(limit, chatTotal) {
  const rest = Math.max(0, limit.percent - chatTotal);
  return `Total used: ${fmtPct(limit.percent)} = (${fmtPct(chatTotal)} by chats in Studio) + (${fmtPct(rest)} elsewhere)`;
}

function overviewBlock(sources) {
  const rows = [];
  for (const source of sources) {
    for (const limit of source.limits) {
      const chatTotal = totalCreditedForLimit(source, limit.key);
      const tone = limit.percent >= 85 ? "hot" : limit.percent >= 60 ? "warn" : "";
      const reset = resetText(limit.resets_at);
      const meta = [splitText(limit, chatTotal), reset].filter(Boolean).join(" · ");
      rows.push(el("div", { class: "limit overview-limit", "data-overview-limit": `${source.key}:${limit.key}` },
        el("div", { class: "limit-name" },
          el("span", { class: "overview-model", text: `${source.label} — ${limit.label}` }),
          el("small", { text: meta })),
        el("div", { class: "bar" }, el("span", { class: tone, style: `width:${Math.min(100, limit.percent)}%` })),
        el("div", { class: "pct", text: fmtPct(limit.percent) })));
    }
  }
  return rows;
}

function limitsBlock(source) {
  const box = el("div", { class: "limits" });
  if (!source.limits.length) {
    box.append(el("div", { class: "empty-note", text: "No reading yet. Click Check now." }));
    return box;
  }
  for (const limit of source.limits) {
    const tone = limit.percent >= 85 ? "hot" : limit.percent >= 60 ? "warn" : "";
    const chatTotal = totalCreditedForLimit(source, limit.key);
    const meta = [splitText(limit, chatTotal), resetText(limit.resets_at)].filter(Boolean).join(" · ");
    box.append(el("div", { class: "limit", "data-limit": limit.key },
      el("div", { class: "limit-name" }, limit.label, el("small", { text: meta })),
      el("div", { class: "bar" }, el("span", { class: tone, style: `width:${Math.min(100, limit.percent)}%` })),
      el("div", { class: "pct", text: fmtPct(limit.percent) })));
  }
  return box;
}

function chart(points, xStart, xEnd, yMax, big) {
  const w = 760, h = big ? 220 : 120, padL = 40, padR = 8, padT = 8, padB = 20;
  const svg = svgEl("svg", { class: "chart", viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: "none", role: "img" });
  const x = (t) => padL + ((t - xStart) / Math.max(1, xEnd - xStart)) * (w - padL - padR);
  const y = (v) => padT + (1 - v / yMax) * (h - padT - padB);
  for (const v of [0, yMax / 2, yMax]) {
    svg.append(svgEl("line", { class: "grid", x1: padL, x2: w - padR, y1: y(v), y2: y(v) }));
    const label = svgEl("text", { class: "axis-label", x: padL - 6, y: y(v) + 4, "text-anchor": "end" });
    label.textContent = fmtPct(v);
    svg.append(label);
  }
  for (const [t, anchor] of [[xStart, "start"], [xEnd, "end"]]) {
    const label = svgEl("text", { class: "axis-label", x: x(t), y: h - 4, "text-anchor": anchor });
    label.textContent = when(new Date(t).toISOString());
    svg.append(label);
  }
  // A running total only steps up: hold each value flat until the next increase.
  let d = `M${x(xStart)},${y(0)}`, last = 0;
  for (const p of points) {
    const t = new Date(p.t).getTime();
    d += ` L${x(t)},${y(last)} L${x(t)},${y(p.value)}`;
    last = p.value;
  }
  d += ` L${x(xEnd)},${y(last)}`;
  svg.append(svgEl("path", { class: "area", d: d + ` L${x(xEnd)},${y(0)} Z` }));
  svg.append(svgEl("path", { class: "line", d }));
  for (const p of points) {
    const dot = svgEl("circle", { class: "dot", cx: x(new Date(p.t).getTime()), cy: y(p.value), r: 3 });
    const title = svgEl("title");
    title.textContent = `${when(p.t)}: ${fmtPct(p.value)} so far`;
    dot.append(title);
    svg.append(dot);
  }
  return svg;
}

// Every chat's running total added together: a point each time any chat's total went up.
function summedSeries(chats, key) {
  const steps = chats
    .flatMap((c) => (c.series[key] || []).map((p) => ({ chat: c.chat_id, t: p.t, value: p.value })))
    .sort((a, b) => new Date(a.t) - new Date(b.t));
  const latest = {};
  const points = [];
  for (const step of steps) {
    latest[step.chat] = step.value;
    const value = Object.values(latest).reduce((sum, v) => sum + v, 0);
    if (points.length && points[points.length - 1].t === step.t) points[points.length - 1].value = value;
    else points.push({ t: step.t, value });
  }
  return points;
}

// One limit's graph and log: every chat's usage of it added together, or the one chosen chat's,
// counted from the limit's last reset (or from the first reading, if it has not reset since).
// A dropdown picks an earlier window instead: the graph and log as they stood when it reset.
function limitChartBlock(source, limit, view) {
  const key = limit.key;
  const all = view.chat === "all";
  const past = limit.past_windows || [];
  const windows = (view.windows ||= {});
  const picked = windows[key] !== undefined && past[windows[key]] ? past[windows[key]] : null;

  let xStart, xEnd, points, total, entries, span;
  if (picked === null) {
    const shown = all ? source.chats : source.chats.filter((c) => c.chat_id === view.chat);
    xEnd = Date.now();
    // From the last reset; else from the first reading of this limit, which may come after tracking began.
    const since = limit.window_started_at || limit.first_read_at || source.tracking_since;
    xStart = since ? Math.min(new Date(since).getTime(), xEnd - 60000) : xEnd - 3600000;
    points = all ? summedSeries(shown, key) : (shown[0] ? shown[0].series[key] || [] : []);
    total = shown.reduce((sum, c) => sum + (c.totals[key] || 0), 0);
    entries = shown
      .flatMap((c) => c.turns.filter((t) => (t.credits[key] || 0) > 0).map((t) => ({ chat: c, turn: t, credit: t.credits[key] })))
      .sort((a, b) => (a.turn.started_at < b.turn.started_at ? 1 : -1));
    span = [
      limit.window_started_at ? `Since it last reset, ${when(limit.window_started_at)}` : (limit.first_read_at ? `Since ${source.label} started reporting it, ${when(since)}` : (since ? `Since tracking began, ${when(since)}` : "")),
      resetText(limit.resets_at),
    ].filter(Boolean).join(" · ");
  } else {
    const chats = all ? picked.chats : picked.chats.filter((c) => c.chat_id === view.chat);
    xStart = new Date(picked.started_at).getTime();
    xEnd = Math.max(new Date(picked.ended_at).getTime(), xStart + 60000);
    points = all
      ? summedSeries(chats.map((c) => ({ chat_id: c.chat_id, series: { [key]: c.points } })), key)
      : (chats[0] ? chats[0].points : []);
    total = chats.reduce((sum, c) => sum + c.total, 0);
    entries = chats
      .flatMap((c) => c.turns.map((t) => ({ chat: c, turn: t, credit: t.credit })))
      .sort((a, b) => (a.turn.started_at < b.turn.started_at ? 1 : -1));
    span = `${source.label} reported up to ${fmtPct(picked.level)} used before it reset`;
  }
  const yMax = [1, 2, 5, 10, 20, 50, 100, 200, 500].find((step) => step >= total * 1.05) || total * 1.1;

  const box = el("div", { class: "single limit-chart", "data-limit-chart": key, "data-chat": view.chat });
  const name = el("span", { class: "chat-name" }, limit.label);
  if (past.length) {
    const select = el("select", { class: "window-pick", "aria-label": `Which window of ${limit.label} to show` });
    select.append(el("option", { value: "current", text: "Current window" }));
    past.forEach((w, i) => select.append(el("option", { value: String(i), text: `${when(w.started_at)} to ${when(w.ended_at)}` })));
    select.value = picked === null ? "current" : String(windows[key]);
    select.addEventListener("change", () => {
      if (select.value === "current") delete windows[key]; else windows[key] = Number(select.value);
      render(lastData);
    });
    name.append(select);
  }
  name.append(el("small", { class: "chart-span", text: span }));
  box.append(el("div", { class: "chat-head" },
    name,
    el("span", { class: "chat-total" }, el("b", { text: fmtPct(total) }), picked === null ? ` of the limit` : ` of the limit, by chats here`)));
  const body = el("div", { class: "chat-body" }, chart(points, xStart, xEnd, yMax, true));
  const turns = el("div", { class: "turns" }, el("h4", { text: "What used it" }));
  if (!entries.length) {
    turns.append(el("div", { class: "empty-note", text: picked !== null
      ? `Nothing was credited to ${all ? "the chats here" : "this chat"} in this window.`
      : `Nothing credited ${limit.window_started_at ? "since the reset" : "yet"}. Usage shows up here after ${source.label}'s number goes up while ${all ? "a chat" : "this chat"} is working.` }));
  } else {
    for (const { chat, turn, credit } of entries) {
      turns.append(el("div", { class: all ? "turn with-chat" : "turn" },
        el("span", { class: "when", text: when(turn.started_at) }),
        turnLabel(turn, `${key}:${chat.chat_id}:${turn.started_at}`),
        all ? el("span", { class: "turn-chat", title: chat.name, text: chat.name }) : null,
        el("span", { class: "num", text: "+" + fmtPct(credit) })));
    }
  }
  body.append(turns);
  box.append(body);
  return box;
}

function byChatBlock(source, view) {
  const block = el("div", { class: "bychat" });
  block.append(el("div", { class: "bychat-head" }, el("h3", { text: "Usage by chat" })));
  block.append(el("p", { class: "hint", text:
    `Estimated running total used by the chats on this account, one graph per limit. ` +
    `Each time ${source.label}'s usage goes up, the increase is credited to the chats that were working; when several were working at once, it is split evenly. ` +
    `Each graph starts over when its limit resets.` }));

  const chips = el("div", { class: "chips" });
  const options = [["all", "All chats"], ...source.chats.map((c) => [c.chat_id, c.name])];
  if (!options.some(([id]) => id === view.chat)) view.chat = "all";
  for (const [id, name] of options) {
    const chip = el("button", { class: "chip", type: "button", "aria-pressed": String(view.chat === id), "data-chat": id, text: name });
    chip.addEventListener("click", () => { view.chat = id; render(lastData); });
    chips.append(chip);
  }
  block.append(chips);

  if (!source.chats.length) {
    block.append(el("div", { class: "single" }, el("div", { class: "empty-note", text: "No chats on this account yet." })));
    return block;
  }
  if (!source.limits.length) {
    block.append(el("div", { class: "single" }, el("div", { class: "empty-note", text: "No reading yet, so there is nothing to graph." })));
    return block;
  }
  for (const limit of source.limits) block.append(limitChartBlock(source, limit, view));
  return block;
}

function sourceSection(source) {
  const view = (views[source.key] ||= { chat: "all" });
  const collapseKey = `source:${source.key}`;
  const name = el("div", { class: "tool-name" }, source.label,
    source.plan ? el("span", { class: "plan", text: `${source.plan} plan` }) : null,
    el("span", { class: "account", text: source.account_label }));
  const link = el("a", { href: source.usage_page, target: "_blank", rel: "noopener", text: `Open ${source.label} usage page` });
  link.addEventListener("click", (e) => e.stopPropagation());
  const summary = el("summary", { class: "tool-head" }, name, el("div", { class: "links" }, link));
  const body = el("div", { class: "tool-body" });
  if (source.error) body.append(el("p", { class: "error", text: source.error }));
  body.append(limitsBlock(source));
  body.append(byChatBlock(source, view));
  const rawKey = `raw:${source.key}`;
  if (!view.rawInitialized) {
    collapsed.add(rawKey);
    view.rawInitialized = true;
  }
  const raw = makeCollapsible("details", rawKey, { class: "raw" },
    el("summary", { text: "View raw reading" }),
    el("pre", { text: source.raw_reading ? JSON.stringify(source.raw_reading, null, 2) : "No reading yet." }));
  body.append(raw);
  return makeCollapsible(
    "details",
    collapseKey,
    { class: "tool", id: `source-${source.key.replace(/[^a-z0-9]/gi, "-")}`, "data-source": source.key },
    summary,
    body
  );
}

function render(data) {
  lastData = data;
  const checked = data.sources.map((s) => s.checked_at).filter(Boolean).sort().pop();
  $("checked").textContent = checked ? `Checked ${ago(checked)}` : "Not checked yet";
  const overview = $("overview");
  const overviewRows = overviewBlock(data.sources);
  if (overviewRows.length) {
    overview.hidden = false;
    overview.replaceChildren(
      el("div", { class: "overview-head" },
        el("h2", { text: "All usage limits" }),
        el("span", { class: "overview-sub", text: "Totaled across all chats" })),
      el("div", { class: "limits" }, ...overviewRows)
    );
  } else {
    overview.hidden = true;
    overview.replaceChildren();
  }
  const box = $("sources");
  box.replaceChildren(...data.sources.map(sourceSection));
  if (!data.sources.length) box.append(el("p", { class: "empty-note", text: "Looking for the chats connected to Studio..." }));
  $("untracked").hidden = !data.untracked_chats.length;
  $("untracked-text").textContent = data.untracked_chats.length
    ? `${data.untracked_chats.map((c) => c.name).join(", ")}: this tracker doesn't know where their AI reports usage yet.`
    : "";
}

async function load() {
  const response = await fetch("/api/usage");
  if (!response.ok) return;
  const data = await response.json();
  // A redraw would close a window dropdown the user has open; the next poll catches up.
  if (document.activeElement && document.activeElement.classList.contains("window-pick")) return;
  render(data);
  // Right after the tracker starts it is still listing the chats; look again shortly.
  if (!data.sources.length) setTimeout(load, 3000);
}

$("check-now").addEventListener("click", async () => {
  const button = $("check-now");
  button.disabled = true;
  button.textContent = "Checking...";
  try {
    const response = await fetch("/api/check", { method: "POST" });
    if (response.ok) render(await response.json());
  } finally {
    button.disabled = false;
    button.textContent = "Check now";
  }
});

load();
// Often enough that a renamed or newly created chat shows up soon after the tracker notices it.
setInterval(load, 10000);
// Coming back to the window shows current names and numbers at once.
document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
