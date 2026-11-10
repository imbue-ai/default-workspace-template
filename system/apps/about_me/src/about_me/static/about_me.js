// The What Studio knows about me page: reads /api/profile and draws each fact with its source, date and Delete,
// the "Forget all of this" control with its confirm, the empty state, and the raw files. Everything is drawn with
// textContent, never as markup, since the facts come from web pages. It talks only to this app's own routes.

const HTTP_CONFLICT = 409;

const elements = {
  notice: document.getElementById("notice"),
  factsSection: document.getElementById("facts-section"),
  factList: document.getElementById("fact-list"),
  forgetAll: document.getElementById("forget-all"),
  forgetConfirm: document.getElementById("forget-confirm"),
  forgetConfirmYes: document.getElementById("forget-confirm-yes"),
  forgetConfirmCancel: document.getElementById("forget-confirm-cancel"),
  emptyState: document.getElementById("empty-state"),
  rawFiles: document.getElementById("raw-files"),
  rawProfile: document.getElementById("raw-profile"),
  rawSources: document.getElementById("raw-sources"),
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

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "fact-delete";
  remove.dataset.variant = "ghost";
  remove.textContent = "Delete";
  remove.setAttribute("aria-label", `Delete: ${fact.text}`);
  remove.addEventListener("click", () => deleteFact(fact.id, remove));

  row.append(body, remove);
  return row;
}

function render(profile) {
  const hasFacts = profile.facts.length > 0;
  elements.factList.replaceChildren(...profile.facts.map(factRow));
  elements.factsSection.hidden = !hasFacts;
  elements.emptyState.hidden = hasFacts;
  closeForgetConfirm();

  const hasRaw = profile.profile_markdown !== null || profile.sources_json !== null;
  elements.rawFiles.hidden = !hasRaw;
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
      return;
    }
    if (!ok) {
      showNotice(body.detail ?? "That did not work; nothing was changed.");
      return;
    }
    showNotice("");
    render(body);
  } catch {
    showNotice("That did not work; nothing was changed.");
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

elements.forgetAll.addEventListener("click", openForgetConfirm);
elements.forgetConfirmCancel.addEventListener("click", closeForgetConfirm);
elements.forgetConfirmYes.addEventListener("click", () => post("/api/forget", {}, elements.forgetConfirmYes));

load().catch(() => showNotice("Could not read what I remember."));
