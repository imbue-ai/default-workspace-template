// The Studio Discord window. Reads /api/state for which view to show, then /api/messages once connected.
const $ = (selector) => document.querySelector(selector);
const STATUS = { online: ["var(--online)", "online"], idle: ["var(--idle)", "away"], dnd: ["var(--dnd)", "busy"] };
const ARROW =
  '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 3h7v7M13 3 5 11" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';

let current = null; // the last /api/state
let waitTimer = null;

function setView(view) {
  document.body.dataset.state = view;
  document.body.classList.toggle("connectedish", ["connected", "notjoined", "error"].includes(view));
  clearInterval(waitTimer);
  waitTimer = view === "waiting" ? setInterval(loadState, 4000) : null;
}

function el(tag, attributes = {}, text = null) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  if (text !== null) node.textContent = text;
  return node;
}

function initials(name) {
  return (name || "?").split(/\s+/).map((part) => part[0]).join("").slice(0, 2).toUpperCase();
}

function when(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  const now = new Date();
  const time = date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const days = Math.floor((new Date(now.toDateString()) - new Date(date.toDateString())) / 86400000);
  if (days === 0) return `today at ${time}`;
  if (days === 1) return `yesterday at ${time}`;
  if (days < 7) return `${date.toLocaleDateString([], { weekday: "long" })} at ${time}`;
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

function renderPresence(server) {
  const box = $("#presence");
  if (!server) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  $("#online-count").textContent = server.online;
  const faces = $("#faces");
  faces.replaceChildren();
  for (const face of server.faces) {
    const [color, word] = STATUS[face.status] || STATUS.online;
    const item = el("li", { title: `${face.name} (${word})` });
    item.append(el("img", { alt: `${face.name}, ${word}`, src: face.avatar }));
    const dot = el("i");
    dot.style.background = color;
    item.append(dot);
    faces.append(item);
  }
}

function renderItem(item) {
  const link = el("a", { class: "msg", href: item.url, target: "_blank", rel: "noopener", "data-message-id": item.id });
  const avatar = el("span", { class: "av", "aria-hidden": "true" });
  if (item.avatar) avatar.append(el("img", { alt: "", src: item.avatar }));
  else avatar.textContent = initials(item.author || item.title);
  const body = el("span");
  const meta = el("span", { class: "meta" });
  if (item.author) meta.append(el("b", {}, item.author));
  if (item.time) meta.append(el("span", {}, when(item.time)));
  if (item.replies !== null && item.replies !== undefined)
    meta.append(el("span", {}, item.replies === 1 ? "1 reply" : `${item.replies} replies`));
  if (item.attachments) meta.append(el("span", {}, item.attachments === 1 ? "1 attachment" : `${item.attachments} attachments`));
  const text = el("span", { class: "text" });
  if (item.title) {
    text.append(el("b", {}, item.title));
    if (item.text) text.append(document.createTextNode(` — ${item.text}`));
  } else {
    text.textContent = item.text || (item.attachments ? "" : "(no text)");
  }
  body.append(meta, text);
  link.append(avatar, body);
  const row = el("li");
  row.append(link);
  return row;
}

function renderChannels(channels, withMessages) {
  const list = $("#channels");
  list.replaceChildren();
  for (const channel of channels) {
    const row = el("li", { class: "channel", id: `ch-${channel.name}`, "data-channel-id": channel.id });
    const head = el("a", { class: "chrow", href: channel.url, target: "_blank", rel: "noopener" });
    const name = el("span", { class: "name" });
    name.append(el("span", { class: "hash" }, "#"), document.createTextNode(channel.name));
    const go = el("span", { class: "go" });
    go.innerHTML = `<span class="label">Open</span>${ARROW}`;
    head.append(name, go, el("span", { class: "desc" }, channel.topic));
    row.append(head);
    if (withMessages) {
      if (channel.items.length) {
        const messages = el("ul", { class: "messages" });
        for (const item of channel.items) messages.append(renderItem(item));
        row.append(messages);
      } else {
        row.append(el("p", { class: "empty" }, channel.kind === "forum" ? "No posts yet." : "No messages yet."));
      }
      const footer = el("div", { class: "ftr" });
      footer.append(
        el("a", { class: "more", href: channel.url, target: "_blank", rel: "noopener" }, "More in Discord"),
        el("a", { class: "rawlink", href: `/raw/${channel.id}`, title: "What Discord returned, unprocessed" }, "Raw"),
      );
      row.append(footer);
    }
    list.append(row);
  }
}

async function call(method, path) {
  const response = await fetch(path, { method });
  return response.json();
}

async function loadMessages() {
  $("#updated").textContent = "loading";
  let data;
  try {
    data = await call("GET", "/api/messages");
  } catch {
    data = { joined: true, problem: "unreachable", loaded_at: null, channels: current.channels };
  }
  if (!data.joined) {
    $("#updated").textContent = "not in the server yet";
    setView("notjoined");
    return;
  }
  renderChannels(data.channels, true);
  if (data.problem) {
    setView("error");
    $("#error-detail").textContent = data.loaded_at ? `Last loaded ${when(data.loaded_at)}.` : "Nothing loaded yet.";
    $("#updated").textContent = data.loaded_at ? when(data.loaded_at) : "not loaded";
  } else {
    setView("connected");
    $("#updated").textContent = "just now";
  }
}

async function loadState() {
  const state = await call("GET", "/api/state");
  current = state;
  for (const link of document.querySelectorAll("a.join")) link.href = state.invite;
  const firstChannel = state.channels[0]?.url || "https://discord.com/app";
  for (const link of document.querySelectorAll("a.open-discord")) link.href = firstChannel;
  renderPresence(state.server);
  $("#note").textContent = state.access_granted_but_disconnected
    ? "Read-only. Studio still holds the Discord access; remove it in Studio's permissions."
    : "Read-only. Only these three channels.";
  if (state.view === "connected") {
    $("#account-name").textContent = state.account ? state.account.name : "Discord";
    if (document.body.dataset.state !== "connected" && document.body.dataset.state !== "error") {
      renderChannels(state.channels, true);
      setView("connected");
      await loadMessages();
    }
    return;
  }
  renderChannels(state.channels, false);
  if (document.body.dataset.state !== "confirm") setView(state.view);
}

async function act(path) {
  const result = await call("POST", path);
  if (result.error) {
    $("#note").textContent = "Couldn't reach Studio. Try again in a moment.";
    return;
  }
  document.body.dataset.state = "";
  await loadState();
}

$("#connect").addEventListener("click", () => act("/api/connect"));
$("#ask-again").addEventListener("click", () => act("/api/connect"));
$("#cancel").addEventListener("click", () => act("/api/cancel"));
$("#disconnect").addEventListener("click", () => act("/api/disconnect"));
$("#refresh").addEventListener("click", loadMessages);
$("#retry").addEventListener("click", loadMessages);
$("#uninstall").addEventListener("click", () => setView("confirm"));
$("#keep").addEventListener("click", () => setView(current ? current.view : "offer"));
$("#confirm-uninstall").addEventListener("click", () => act("/api/uninstall"));
$("#undo").addEventListener("click", () => act("/api/undo-uninstall"));

loadState();
