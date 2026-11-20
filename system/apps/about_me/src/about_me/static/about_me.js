// The What Studio knows about me page: reads /api/profile and draws every memory the agent keeps, grouped by what it
// is about, each with Edit and Delete; the web lookup's facts with their sources; the box to add a memory; the two
// forget controls; the empty state; and the raw files. Everything is drawn with textContent, never as markup, since
// memories quote web pages and what people typed. It talks only to this app's own routes.

const HTTP_CONFLICT = 409;
// How often the page re-reads the memory while it is on screen, so changes the agent or the Files app make show up.
const REFRESH_INTERVAL_MS = 5000;

// The last profile drawn, as JSON, so a refresh that finds nothing new draws nothing.
let drawnProfile = "";

// The groups, in page order: the memory type each holds and its heading.
const GROUPS = [
  ["user", "About you"],
  ["feedback", "How you like to work"],
  ["project", "Your projects"],
  ["reference", "Links and references"],
  ["other", "Other notes"],
];

const elements = {
  notice: document.getElementById("notice"),
  groups: document.getElementById("groups"),
  forgetControls: document.getElementById("forget-controls"),
  forgetAll: document.getElementById("forget-all"),
  forgetConfirm: document.getElementById("forget-confirm"),
  forgetConfirmYes: document.getElementById("forget-confirm-yes"),
  forgetConfirmCancel: document.getElementById("forget-confirm-cancel"),
  emptyState: document.getElementById("empty-state"),
  rawFiles: document.getElementById("raw-files"),
  rawIndex: document.getElementById("raw-index"),
  rawProfile: document.getElementById("raw-profile"),
  rawSources: document.getElementById("raw-sources"),
  addMemory: document.getElementById("add-memory"),
  addMemoryText: document.getElementById("add-memory-text"),
  addMemorySubmit: document.getElementById("add-memory-submit"),
};

function showNotice(text) {
  elements.notice.textContent = text;
  elements.notice.hidden = text === "";
}

function formatDate(isoDate) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate);
  if (match === null) return isoDate;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function prettySources(raw) {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

function factRow(fact) {
  const row = document.createElement("li");
  row.className = "fact";
  row.dataset.factId = fact.id;

  const body = document.createElement("div");
  body.className = "fact-body";
  const text = document.createElement("p");
  text.className = "fact-text";
  text.textContent = fact.text;
  body.append(text);

  const meta = document.createElement("p");
  meta.className = "fact-meta";
  if (fact.source_url !== null) {
    const link = document.createElement("a");
    link.href = fact.source_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = fact.source_title || fact.source_url;
    meta.append(link);
  } else if (fact.is_told_by_user) {
    meta.append("You told me");
  }
  if (fact.found_on !== null) {
    if (meta.childNodes.length > 0) meta.append(" · ");
    meta.append(formatDate(fact.found_on));
  }
  if (meta.childNodes.length > 0) body.append(meta);

  const edit = document.createElement("button");
  edit.type = "button";
  edit.className = "fact-edit";
  edit.dataset.variant = "ghost";
  edit.textContent = "Edit";
  edit.setAttribute("aria-label", `Edit: ${fact.text}`);

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "fact-delete";
  remove.dataset.variant = "ghost";
  remove.textContent = "Delete";
  remove.setAttribute("aria-label", `Delete: ${fact.text}`);
  remove.addEventListener("click", () => deleteFact(fact.id, remove));

  const actions = document.createElement("div");
  actions.className = "fact-actions";
  actions.append(edit, remove);
  edit.addEventListener("click", () => startEditing(row, fact, text, actions));

  row.append(body, actions);
  return row;
}

// Swaps the fact's words for a box holding them, with Save and Cancel in place of Edit and Delete.
function startEditing(row, fact, text, actions) {
  const input = document.createElement("input");
  input.type = "text";
  input.className = "fact-edit-input";
  input.maxLength = 500;
  input.value = fact.text;
  input.setAttribute("aria-label", "Fact");

  const save = document.createElement("button");
  save.type = "button";
  save.textContent = "Save";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.dataset.variant = "ghost";
  cancel.textContent = "Cancel";
  const editing = document.createElement("div");
  editing.className = "fact-actions";
  editing.append(save, cancel);

  function stopEditing() {
    input.replaceWith(text);
    editing.replaceWith(actions);
  }

  async function saveEdit() {
    const words = input.value.trim();
    if (words === "") {
      input.focus();
      return;
    }
    if (words === fact.text) {
      stopEditing();
      return;
    }
    await post("/api/facts/edit", { fact_id: fact.id, text: words }, save);
  }

  save.addEventListener("click", saveEdit);
  cancel.addEventListener("click", stopEditing);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      saveEdit();
    } else if (event.key === "Escape") {
      stopEditing();
    }
  });

  text.replaceWith(input);
  actions.replaceWith(editing);
  input.focus();
  input.select();
}


function button(label, variant) {
  const control = document.createElement("button");
  control.type = "button";
  control.textContent = label;
  if (variant !== undefined) control.dataset.variant = variant;
  return control;
}

function memoryCard(memory) {
  const card = document.createElement("li");
  card.className = "memory";
  card.dataset.fileName = memory.file_name;

  const body = document.createElement("div");
  body.className = "memory-body";
  const title = document.createElement("p");
  title.className = "memory-title";
  title.textContent = memory.title;
  const text = document.createElement("p");
  text.className = "memory-text";
  text.textContent = memory.body;
  body.append(title);
  // A memory added on this page is titled with its own words; showing them twice says nothing more.
  if (memory.body.trim() !== memory.title) body.append(text);

  const edit = button("Edit", "ghost");
  edit.setAttribute("aria-label", `Edit: ${memory.title}`);
  const remove = button("Delete", "ghost");
  remove.setAttribute("aria-label", `Delete: ${memory.title}`);
  remove.addEventListener("click", () =>
    post("/api/memories/delete", { file_name: memory.file_name, revision: memory.revision }, remove),
  );
  const actions = document.createElement("div");
  actions.className = "fact-actions";
  actions.append(edit, remove);
  edit.addEventListener("click", () => startEditingMemory(card, memory, body, actions));

  card.append(body, actions);
  return card;
}

// Swaps a memory for a form holding its summary and text, with Save and Cancel.
function startEditingMemory(card, memory, body, actions) {
  const form = document.createElement("form");
  form.className = "memory-form";
  const summaryLabel = document.createElement("label");
  summaryLabel.textContent = "Summary";
  const summary = document.createElement("input");
  summary.type = "text";
  summary.maxLength = 300;
  summary.value = memory.description ?? memory.title;
  summaryLabel.append(summary);
  const textLabel = document.createElement("label");
  textLabel.textContent = "What I remember";
  const text = document.createElement("textarea");
  text.rows = Math.min(10, Math.max(3, memory.body.split("\n").length + 1));
  text.maxLength = 5000;
  text.value = memory.body;
  textLabel.append(text);
  const save = button("Save");
  save.type = "submit";
  const cancel = button("Cancel", "ghost");
  const editing = document.createElement("div");
  editing.className = "fact-actions";
  editing.append(save, cancel);
  form.append(summaryLabel, textLabel, editing);

  function stopEditing() {
    form.replaceWith(body);
    actions.hidden = false;
  }

  cancel.addEventListener("click", stopEditing);
  form.addEventListener("keydown", (event) => {
    if (event.key === "Escape") stopEditing();
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    post(
      "/api/memories/edit",
      { file_name: memory.file_name, revision: memory.revision, description: summary.value, body: text.value },
      save,
    );
  });

  body.replaceWith(form);
  actions.hidden = true;
  text.focus();
}

function lookupBlock(facts) {
  const block = document.createElement("div");
  block.className = "lookup";
  const heading = document.createElement("h3");
  heading.textContent = "From the web lookup you asked for";
  const list = document.createElement("ul");
  list.className = "fact-list";
  list.replaceChildren(...facts.map(factRow));
  const forget = button("Forget what the lookup found", "ghost");
  forget.addEventListener("click", () => post("/api/forget", { scope: "lookup" }, forget));
  block.append(heading, list, forget);
  return block;
}

function group(heading, memories, facts) {
  const section = document.createElement("section");
  section.className = "group";
  const title = document.createElement("h2");
  title.textContent = heading;
  section.append(title);
  if (facts.length > 0) section.append(lookupBlock(facts));
  if (memories.length > 0) {
    const list = document.createElement("ul");
    list.className = "memory-list";
    list.replaceChildren(...memories.map(memoryCard));
    section.append(list);
  }
  return section;
}

function render(profile) {
  drawnProfile = JSON.stringify(profile);
  const sections = GROUPS.flatMap(([type, heading]) => {
    const memories = profile.memories.filter((memory) => memory.memory_type === type);
    const facts = type === "user" ? profile.facts : [];
    return memories.length > 0 || facts.length > 0 ? [group(heading, memories, facts)] : [];
  });
  elements.groups.replaceChildren(...sections);
  const hasAny = sections.length > 0;
  elements.emptyState.hidden = hasAny;
  elements.forgetControls.hidden = !hasAny;
  closeForgetConfirm();

  const hasRaw = profile.memory_index_markdown !== null || profile.profile_markdown !== null || profile.sources_json !== null;
  elements.rawFiles.hidden = !hasRaw;
  elements.rawIndex.textContent = profile.memory_index_markdown ?? "(no file)";
  elements.rawProfile.textContent = profile.profile_markdown ?? "(no file)";
  elements.rawSources.textContent = profile.sources_json === null ? "(no file)" : prettySources(profile.sources_json);
}

async function request(path, init) {
  const response = await fetch(path, { cache: "no-store", ...init });
  const body = await response.json();
  return { status: response.status, ok: response.ok, body };
}

async function load() {
  const { ok, body } = await request("/api/profile");
  if (!ok) {
    showNotice(body.detail ?? "Could not read what I remember.");
    return;
  }
  render(body);
}

// Whether the change went through; the page then shows the profile the server answered.
async function post(path, payload, control) {
  control.disabled = true;
  try {
    const { status, ok, body } = await request(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (status === HTTP_CONFLICT) {
      showNotice(body.detail);
      await load();
      return false;
    }
    if (!ok) {
      showNotice(body.detail ?? "That did not work; nothing was changed.");
      return false;
    }
    showNotice("");
    render(body);
    return true;
  } catch {
    showNotice("That did not work; nothing was changed.");
    return false;
  } finally {
    control.disabled = false;
  }
}

function deleteFact(factId, control) {
  return post("/api/facts/delete", { fact_id: factId }, control);
}

function openForgetConfirm() {
  elements.forgetAll.hidden = true;
  elements.forgetConfirm.hidden = false;
  elements.forgetConfirmCancel.focus();
}

function closeForgetConfirm() {
  elements.forgetConfirm.hidden = true;
  elements.forgetAll.hidden = false;
}

async function addMemory(event) {
  event.preventDefault();
  const text = elements.addMemoryText.value.trim();
  if (text === "") {
    elements.addMemoryText.focus();
    return;
  }
  if (await post("/api/memories/add", { text }, elements.addMemorySubmit)) {
    elements.addMemoryText.value = "";
  }
}

elements.addMemory.addEventListener("submit", addMemory);
elements.forgetAll.addEventListener("click", openForgetConfirm);
elements.forgetConfirmCancel.addEventListener("click", closeForgetConfirm);
elements.forgetConfirmYes.addEventListener("click", () =>
  post("/api/forget", { scope: "everything" }, elements.forgetConfirmYes),
);

// Whether redrawing now would throw away something the person is doing: an open edit or the forget confirm.
function isBusy() {
  return document.querySelector(".memory-form, .fact-edit-input") !== null || !elements.forgetConfirm.hidden;
}

async function refresh() {
  if (document.visibilityState !== "visible" || isBusy()) return;
  try {
    const { ok, body } = await request("/api/profile");
    if (!ok || isBusy() || JSON.stringify(body) === drawnProfile) return;
    render(body);
  } catch {
    // A refresh that fails is retried on the next tick; the page keeps what it drew.
  }
}

window.addEventListener("focus", refresh);
document.addEventListener("visibilitychange", refresh);
setInterval(refresh, REFRESH_INTERVAL_MS);

load().catch(() => showNotice("Could not read what I remember."));
