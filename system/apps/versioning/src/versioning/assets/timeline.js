"use strict";

var APP_NAME = window.APP_NAME;
var FEED_CHUNK = 30;

/* ── State ── */

var S = {
  history: null,          // {app, nodes (oldest first), is_restorable}
  apps: [],
  selectedSha: null,
  visibleCount: FEED_CHUNK,
  chatBySha: {},
  restore: { reviewing: false, preview: null, status: null, isError: false, busy: false },
  chatBusy: false,
  tech: { open: false, textBySha: {} },
};

function feedNodes() {
  return S.history ? S.history.nodes.slice().reverse() : [];
}

function nodeBySha(sha) {
  return S.history ? S.history.nodes.find(function (n) { return n.sha === sha; }) : null;
}

function selectNode(sha) {
  S.selectedSha = sha;
  S.restore = { reviewing: false, preview: null, status: null, isError: false, busy: false };
  S.tech.open = false;
  var node = nodeBySha(sha);
  if (node && !node.summary) requestSummary(sha);
}

/* ── API ── */

function api(path) { return "/api/app/" + encodeURIComponent(APP_NAME) + path; }

function loadHistory() {
  return m.request({ url: api("/history") }).then(function (data) {
    S.history = data;
    document.title = "History - " + data.app.title;
    var current = data.nodes.find(function (n) { return n.is_current; });
    if (current) selectNode(current.sha);
  });
}

function loadApps() {
  return m.request({ url: "/api/apps" }).then(function (data) { S.apps = data.apps; });
}

var summaryRequested = {};
function requestSummary(sha) {
  if (summaryRequested[sha]) return;
  summaryRequested[sha] = true;
  m.request({ method: "POST", url: api("/summary/" + encodeURIComponent(sha)) })
    .then(function (summary) {
      var node = nodeBySha(sha);
      if (node) node.summary = summary;
    })
    .catch(function () { /* the raw title is always shown */ });
}

function loadTechRecord(sha) {
  if (S.tech.textBySha[sha]) return;
  m.request({ url: api("/diff/" + encodeURIComponent(sha)) })
    .then(function (data) {
      var header = data.commits.map(function (c) {
        return c.subject + "  (" + c.sha.slice(0, 10) + ")\n\n" + c.body;
      }).join("\n\n");
      S.tech.textBySha[sha] = header + "\n\n" + (data.diff || "(no textual changes)");
    })
    .catch(function () { S.tech.textBySha[sha] = "Could not load the technical record."; });
}

/* ── Icons (inline SVG, stroke = currentColor) ── */

function icon(name, size) {
  var paths = {
    undo: [m("path", { d: "M9 14 4 9l5-5" }), m("path", { d: "M4 9h10a6 6 0 0 1 0 12h-3" })],
    send: [m("path", { d: "m3 11 18-8-8 18-2-8-8-2Z" })],
    chevron: [m("path", { d: "m6 9 6 6 6-6" })],
    history: [
      m("path", { d: "M3 12a9 9 0 1 0 3-6.7L3 8" }),
      m("path", { d: "M3 3v5h5" }),
      m("path", { d: "M12 7v5l4 2" }),
    ],
    app: [m("rect", { x: 3, y: 3, width: 18, height: 18, rx: 4 })],
  };
  return m("svg.icon", {
    width: size || 16, height: size || 16, viewBox: "0 0 24 24",
    fill: "none", stroke: "currentColor",
    "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round",
    "aria-hidden": "true",
  }, paths[name]);
}

/* ── Sidebar: every app, with the one being browsed marked ── */

function sanitizedAppIcon(markup) {
  if (typeof markup !== "string" || !/^\s*<svg[\s>]/i.test(markup) || !window.DOMPurify) return null;
  var clean = DOMPurify.sanitize(markup, {
    USE_PROFILES: { svg: true },
    FORBID_TAGS: [
      "script", "style", "a", "image", "iframe", "foreignObject",
      "animate", "animateMotion", "animateTransform", "set",
    ],
    FORBID_ATTR: ["style", "class", "href", "xlink:href", "src", "xml:base"],
  });
  return /^\s*<svg[\s>]/i.test(clean) ? clean : null;
}

var AppListItem = {
  view: function (vnode) {
    var appRef = vnode.attrs.app;
    var isCurrent = appRef.name === APP_NAME;
    var iconMarkup = sanitizedAppIcon(appRef.icon);
    return m("li", m("a.app-item", {
      class: isCurrent ? "current" : "",
      href: "/app/" + encodeURIComponent(appRef.name),
      title: appRef.title,
      "aria-current": isCurrent ? "page" : undefined,
    }, [
      m("span.app-icon", { "aria-hidden": "true" },
        iconMarkup ? m.trust(iconMarkup) : icon("app", 16)),
      m("span.app-item-label", appRef.title),
    ]));
  },
};

var Sidebar = {
  view: function () {
    return m("nav.sidebar", { "aria-label": "Apps" }, [
      m(".sidebar-head", [icon("history", 16), m("span.sidebar-head-label", "History")]),
      m(".sidebar-group-label", "Apps"),
      m("ul.sidebar-list", S.apps.map(function (a) {
        return m(AppListItem, { key: a.name, app: a });
      })),
    ]);
  },
};

/* ── Page header ── */

var Header = {
  view: function () {
    var h = S.history;
    var count = h ? h.nodes.length : 0;
    return m("header.top-bar", [
      m(".top-title", h ? h.app.title : "Versioning"),
      count > 0 ? m(".top-count", count + " version" + (count === 1 ? "" : "s")) : null,
    ]);
  },
};

/* ── Version titles and day grouping ── */

function baseTitle(node) { return node.summary ? node.summary.title : node.raw_title; }

function dayKey(iso) {
  var d = new Date(iso);
  return d.getFullYear() + "-" + d.getMonth() + "-" + d.getDate();
}
function dayLabel(iso) {
  var d = new Date(iso);
  var now = new Date();
  var startOf = function (x) { return new Date(x.getFullYear(), x.getMonth(), x.getDate()); };
  var diffDays = Math.round((startOf(now) - startOf(d)) / 86400000);
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  var opts = { month: "long", day: "numeric" };
  if (d.getFullYear() !== now.getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString("en-US", opts);
}

var OLDEST_RESTORE_NAME = /^Restored .+ to an earlier version$/;
var PREVIOUS_RESTORE_NAME = /^Went back to (".*"|an earlier version)$/;
var CURRENT_RESTORE_NAME = /^Restored from /;

function isRestoreName(title) {
  return OLDEST_RESTORE_NAME.test(title) || PREVIOUS_RESTORE_NAME.test(title) || CURRENT_RESTORE_NAME.test(title);
}

function restoredFromTitle(targetTitle) {
  return targetTitle ? 'Restored from "' + targetTitle + '"' : "Restored from an earlier version";
}

function rowTitle(node) {
  var title = baseTitle(node);
  if (!node.restored_from_sha) return title;
  var target = nodeBySha(node.restored_from_sha);
  if (target) {
    var targetTitle = baseTitle(target);
    return restoredFromTitle(isRestoreName(targetTitle) ? null : targetTitle);
  }
  var wentBack = PREVIOUS_RESTORE_NAME.exec(title);
  if (wentBack) {
    var quoted = wentBack[1];
    var inner = quoted === "an earlier version" ? null : quoted.slice(1, -1);
    return restoredFromTitle(inner !== null && !isRestoreName(inner) ? inner : null);
  }
  return OLDEST_RESTORE_NAME.test(title) ? restoredFromTitle(null) : title;
}

/* ── Collapsed row ── */

var VersionRow = {
  view: function (vnode) {
    var node = vnode.attrs.node;
    var stateBits = [];
    if (node.is_current) stateBits.push("current version");
    if (node.is_set_aside) stateBits.push("kept safe");
    return m("button.row", {
      class: (node.is_current ? "current " : "") + (node.is_set_aside ? "aside" : ""),
      oncreate: function () { if (!node.summary) requestSummary(node.sha); },
      onclick: function () { selectNode(node.sha); },
      "aria-label": rowTitle(node) + ", " + node.when_label +
                    (stateBits.length ? ", " + stateBits.join(", ") : ""),
      "aria-expanded": "false",
      "aria-current": node.is_current ? "true" : undefined,
    }, [
      m(".node-cell", m(".node")),
      m("span.row-title", rowTitle(node)),
      node.is_current ? m("span.pill.current", "Current") : null,
      node.is_set_aside ? m("span.pill.aside", "Kept safe") : null,
      m("span.row-time", node.short_when_label),
    ]);
  },
};

/* ── Expanded card ── */

function noRestoreReason(node) {
  if (node.is_current) return "This is the version the app is on right now.";
  return S.history.app.title + " can be browsed here, but going back has to be done from a chat.";
}

var VersionCard = {
  view: function (vnode) {
    var node = vnode.attrs.node;
    var canRestore = S.history.is_restorable && !node.is_current;
    var r = S.restore;
    var eyebrow = (node.is_current ? "Current · " : "Viewing · ") + node.when_label;
    return m("article.card", { "aria-label": rowTitle(node) }, [
      m(".card-head", [
        m(".card-eyebrow-row", [
          m(".card-node"),
          m(".card-eyebrow", eyebrow),
        ]),
        m(".card-body", [
          m("h2.card-title", rowTitle(node)),
          node.summary
            ? m("p.card-desc", node.summary.description)
            : m("p.card-desc.pending", "Writing a plain-language description…"),
          node.phrase ? m("p.card-meta", node.phrase) : null,
          m(".card-actions", [
            canRestore ? m("button.btn-restore", {
              onclick: startRestoreReview,
              disabled: r.reviewing || r.busy,
            }, [icon("undo", 15), "Go back to this version"])
              : m(".no-restore-note", noRestoreReason(node)),
            m("button.tech-toggle", {
              "aria-expanded": String(S.tech.open),
              onclick: function () {
                S.tech.open = !S.tech.open;
                if (S.tech.open) loadTechRecord(node.sha);
              },
            }, [icon("chevron", 12), S.tech.open ? "Hide source" : "See source"]),
          ]),
          m(RestoreConfirm),
          r.status ? m("p.status-line", { class: r.isError ? "error" : "", "aria-live": "polite" }, r.status) : null,
          m(".tech-block", { class: S.tech.open ? "open" : "" },
            S.tech.textBySha[node.sha] || "Loading…"),
        ]),
      ]),
      m(VersionChat, { node: node }),
    ]);
  },
};

/* ── Feed ── */

var TimelineFeed = {
  view: function () {
    if (!S.history) return m(".loading", "Reading history…");
    var nodes = feedNodes();
    if (nodes.length === 0) return m(".empty", "No history yet for this app.");
    var shown = nodes.slice(0, S.visibleCount);
    var remaining = nodes.length - shown.length;

    var blocks = [];
    shown.forEach(function (n) {
      var key = dayKey(n.authored_at);
      if (blocks.length === 0 || blocks[blocks.length - 1].key !== key) {
        blocks.push({ key: key, label: dayLabel(n.authored_at), nodes: [] });
      }
      blocks[blocks.length - 1].nodes.push(n);
    });

    return m("div", [
      blocks.map(function (block) {
        return m(".day-block", { key: block.key }, [
          m(".day-label-row", [
            m(".day-label", { class: block.label === "Today" ? "today" : "" }, block.label),
            m(".day-rule"),
          ]),
          m(".day-rows", m("ul.day-list", { "aria-label": "Versions from " + block.label },
            block.nodes.map(function (n) {
              return m("li", { key: n.sha },
                n.sha === S.selectedSha ? m(VersionCard, { node: n }) : m(VersionRow, { node: n }));
            }))),
        ]);
      }),
      remaining > 0 ? m(".feed-sentinel", {
        oncreate: function (vnode) {
          var observer = new IntersectionObserver(function (entries) {
            if (entries.some(function (e) { return e.isIntersecting; })) {
              S.visibleCount += FEED_CHUNK;
              m.redraw();
            }
          });
          observer.observe(vnode.dom);
          vnode.state.observer = observer;
        },
        onremove: function (vnode) { if (vnode.state.observer) vnode.state.observer.disconnect(); },
      }) : null,
      remaining > 0 ? m(".feed-note", remaining + " earlier version" + (remaining === 1 ? "" : "s") + "…") : null,
    ]);
  },
};

/* ── Restore ── */

function startRestoreReview() {
  var r = S.restore;
  r.reviewing = true;
  r.preview = null;
  r.status = null;
  m.request({
    method: "POST", url: api("/restore"),
    body: { sha: S.selectedSha, mode: "preview" },
  }).then(function (preview) { r.preview = preview; })
    .catch(function (e) {
      r.preview = { error: (e.response && e.response.error) || "Could not preview the restore." };
    });
}

var RestoreConfirm = {
  view: function () {
    var r = S.restore;
    return m(".restore-confirm", { class: r.reviewing ? "open" : "" }, [
      m(".stat", { "aria-live": "polite" }, restorePreviewText(r.preview)),
      m(".buttons", [
        m("button.cancel", { onclick: function () { r.reviewing = false; } }, "Cancel"),
        m("button.go", {
          disabled: r.busy || !r.preview || r.preview.error || isPreviewUnchanged(r.preview),
          onclick: function () { applyRestore(S.selectedSha); },
        }, "Go back to this version"),
      ]),
    ]);
  },
};

function isPreviewUnchanged(preview) {
  return preview.changed_file_count === 0 && !preview.is_startup_config_changed;
}

function restorePreviewText(preview) {
  if (!preview) return "Checking what would change…";
  if (preview.error) return preview.error;
  if (isPreviewUnchanged(preview)) return "The app is already identical to that version.";
  var laterNote = preview.set_aside_node_count > 0
    ? " " + preview.set_aside_node_count + " later version" + (preview.set_aside_node_count === 1 ? "" : "s") +
      " will be kept safe on the timeline, so you can always return to them."
    : "";
  if (preview.changed_file_count === 0) {
    return "Going back returns the way the app starts up to how it was." + laterNote;
  }
  var startupNote = preview.is_startup_config_changed
    ? " The way it starts up goes back too, so it runs as it did then."
    : "";
  return "Going back returns " + preview.changed_file_count + " part" +
         (preview.changed_file_count === 1 ? "" : "s") + " of the app to how they were." + startupNote + laterNote;
}

function applyRestore(sha) {
  var r = S.restore;
  r.busy = true;
  r.isError = false;
  r.status = "Going back… the app will restart.";
  m.request({ method: "POST", url: api("/restore"), body: { sha: sha, mode: "apply" } })
    .then(function () {
      r.status = "Done. Reloading the timeline…";
      setTimeout(function () { location.reload(); }, 1200);
    })
    .catch(function (e) {
      r.busy = false;
      r.isError = true;
      r.status = (e.response && e.response.error) || "Going back failed.";
    });
}

/* ── Chat, scoped to the open version ── */

function renderAnswer(text) {
  if (window.marked && window.DOMPurify) {
    return m.trust(DOMPurify.sanitize(marked.parse(text)));
  }
  return text;
}

var STARTERS = [
  { label: "What changed in this version?", message: "What changed in this version?" },
  { label: "Why was this changed?", message: "Why was this change made?" },
];

var VersionChat = {
  view: function (vnode) {
    var node = vnode.attrs.node;
    var exchanges = S.chatBySha[node.sha] || [];
    return m(".card-chat", [
      exchanges.length > 0 ? m(".chat-thread", {
        onupdate: function (v) { v.dom.scrollTop = v.dom.scrollHeight; },
        "aria-live": "polite",
      }, exchanges.flatMap(function (ex) {
        return [
          m(".chat-q", ex.question),
          m(".chat-a", { class: ex.answer ? "" : "pending" },
            ex.answer ? renderAnswer(ex.answer)
                      : m(".typing-dots", { role: "status", "aria-label": "Working on it" },
                          [m("span"), m("span"), m("span")])),
          ex.newVersion ? m(".chat-note", "Saved to the timeline — the app was updated.") : null,
        ];
      })) : m(".chat-starters", STARTERS.map(function (s) {
        return m("button.chip", {
          disabled: S.chatBusy,
          onclick: function () { sendAssistMessage(s.message); },
        }, s.label);
      })),
      m(".chat-form", [
        m("textarea.chat-input", {
          rows: 1,
          placeholder: "Ask about this version…",
          "aria-label": "Ask about this version",
          oninput: function (e) {
            e.target.style.height = "auto";
            e.target.style.height = Math.min(e.target.scrollHeight, 96) + "px";
          },
          onkeydown: function (e) {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendAssist(e.target); }
          },
        }),
        m("button.chat-send", {
          disabled: S.chatBusy,
          "aria-label": "Send",
          onclick: function (e) {
            sendAssist(e.currentTarget.parentNode.querySelector("textarea"));
          },
        }, icon("send", 14)),
      ]),
    ]);
  },
};

function refreshHistory() {
  return m.request({ url: api("/history") }).then(function (data) {
    S.history = data;
    if (!nodeBySha(S.selectedSha)) {
      var current = data.nodes.find(function (n) { return n.is_current; });
      if (current) S.selectedSha = current.sha;
    }
  });
}

function sendAssist(field) {
  var message = field.value.trim();
  if (!message || !S.selectedSha || S.chatBusy) return;
  field.value = "";
  field.style.height = "auto";
  sendAssistMessage(message);
}

function sendAssistMessage(message) {
  var sha = S.selectedSha;
  if (!message || !sha || S.chatBusy) return;
  if (!S.chatBySha[sha]) S.chatBySha[sha] = [];
  var exchange = { question: message, answer: null, newVersion: null };
  S.chatBySha[sha].push(exchange);
  S.chatBusy = true;
  var prior = S.chatBySha[sha]
    .filter(function (ex) { return ex.answer; })
    .map(function (ex) { return { question: ex.question, answer: ex.answer }; });
  m.request({
    method: "POST", url: api("/assist"),
    body: { sha: sha, message: message, prior: prior },
  }).then(function (started) {
    pollAssist(started.job_id, exchange);
  }).catch(function (e) {
    exchange.answer = (e.response && e.response.error) || "Sorry, that didn't work right now.";
    S.chatBusy = false;
  });
}

function pollAssist(jobId, exchange) {
  var timer = setInterval(function () {
    m.request({ url: api("/assist/" + encodeURIComponent(jobId)) })
      .then(function (job) {
        if (job.status === "running") return;
        clearInterval(timer);
        S.chatBusy = false;
        if (job.status === "done") {
          exchange.answer = job.answer || "Done.";
          if (job.new_version_sha) {
            exchange.newVersion = job.new_version_sha;
            refreshHistory();
          }
        } else {
          exchange.answer = job.answer || "Sorry, that didn't work right now.";
        }
      })
      .catch(function () { /* keep polling */ });
  }, 3000);
}

/* ── Root ── */

var App = {
  view: function () {
    return [m(Sidebar), m("main.main", m(".page", [m(Header), m(TimelineFeed)]))];
  },
};

document.addEventListener("keydown", function (e) {
  if (e.target.tagName === "TEXTAREA" || e.target.tagName === "INPUT") return;
  if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
  var nodes = feedNodes();
  var idx = nodes.findIndex(function (n) { return n.sha === S.selectedSha; });
  if (idx < 0) return;
  var next = e.key === "ArrowDown" ? Math.min(idx + 1, nodes.length - 1) : Math.max(idx - 1, 0);
  if (next !== idx) {
    e.preventDefault();
    if (next >= S.visibleCount) S.visibleCount = next + FEED_CHUNK;
    selectNode(nodes[next].sha);
    m.redraw();
    requestAnimationFrame(function () {
      var el = document.querySelector(".card");
      if (!el) return;
      var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      el.scrollIntoView({ block: "nearest", behavior: reduce ? "auto" : "smooth" });
    });
  }
});

m.mount(document.getElementById("root"), App);
loadApps();
loadHistory();
if (window.parent !== window) {
  window.parent.postMessage({ type: "minds-location", path: "/app/" + APP_NAME }, "*");
}
