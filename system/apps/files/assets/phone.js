// minds patch: the phone layout (docs/system/blueprint/desktop-interface/plan-phone-interface.md,
// "The file viewer"). Under 700px of the frame's own width, the Name / Last Modified / Size /
// Actions table and the toolbox head give way to a header (up, the folder's name, search, and a
// kebab holding the toolbox verbs), a scrollable breadcrumb strip, sort keys, and one row per
// entry with that entry's actions behind a kebab. The editor page gets the same header with a
// Save button and the file's kebab.
//
// Everything runs on dufs's own state and functions -- DATA, PARAMS, newUrl, movePath,
// deletePath, saveChange, the toolbox controls and their handlers, the breadcrumb and search
// bar it built -- so the rows and the table always agree, and dufs's own prompts and confirms
// are the ones the user answers. This file and phone.css are the whole patch: index.html only
// includes them, so a dufs bump re-applies two lines.

const PHONE_QUERY = window.matchMedia("(max-width: 700px)");
// On <html>, valued with DATA.kind so phone.css can lay the editor page out as a column.
const PHONE_ATTRIBUTE = "data-files-phone";

const PHONE_ICONS = {
  up: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 6l-6 6 6 6"/></svg>`,
  search: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>`,
  kebab: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="5" r="2" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="2" fill="currentColor" stroke="none"/><circle cx="12" cy="19" r="2" fill="currentColor" stroke="none"/></svg>`,
  close: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6L6 18"/><path d="M6 6l12 12"/></svg>`,
};

const phone = {
  $top: null,
  $list: null,
  $menuLayer: null,
  $menuTrigger: null,
  // dufs nodes lent to the phone layout, with where each goes back to.
  borrowed: [],
  isSearchOpen: Boolean(PARAMS.q),
  // The editor's text as loaded, so Save is offered only for a change.
  savedText: null,
};

// dufs calls ready() from its DOMContentLoaded handler by name, so wrapping it here runs the
// phone layout once dufs has rendered the page, the table, and the editor's loaded text.
const dufsReady = ready;
ready = async function () {
  await dufsReady();
  setupPhoneLayout();
};

function setupPhoneLayout() {
  if (DATA.kind === "Edit" && DATA.editable) {
    phone.savedText = $editor.value;
    $editor.addEventListener("input", updateSaveButton);
  }
  applyPhoneLayout(PHONE_QUERY.matches);
  PHONE_QUERY.addEventListener("change", (event) => applyPhoneLayout(event.matches));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closePhoneMenu();
  });
}

function applyPhoneLayout(isPhone) {
  if (isPhone === document.documentElement.hasAttribute(PHONE_ATTRIBUTE)) return;
  if (isPhone) {
    mountPhoneLayout();
  } else {
    unmountPhoneLayout();
  }
}

function mountPhoneLayout() {
  document.documentElement.setAttribute(PHONE_ATTRIBUTE, DATA.kind);

  const $top = document.createElement("div");
  $top.className = "phone-top";
  $top.innerHTML = `
    <div class="phone-hdr"></div>
    <div class="phone-crumb"></div>
    <div class="phone-search hidden">
      <button type="button" class="phone-btn phone-search-close" data-phone-act="search-close" aria-label="Close search">${PHONE_ICONS.close}</button>
    </div>
    <div class="phone-sort hidden"></div>`;
  $top.addEventListener("click", onPhoneClick);
  document.body.prepend($top);
  phone.$top = $top;

  const $crumb = $top.querySelector(".phone-crumb");
  borrowNode(document.querySelector(".breadcrumb"), $crumb);

  if (DATA.kind === "Index") {
    const $searchbar = document.querySelector(".searchbar");
    if (!$searchbar.classList.contains("hidden")) {
      const $searchStrip = $top.querySelector(".phone-search");
      borrowNode($searchbar, $searchStrip, $searchStrip.firstElementChild);
      document.getElementById("search").placeholder = `Search in ${folderTitle(DATA.href)}`;
    }
    renderSortKeys($top.querySelector(".phone-sort"));

    const $list = document.createElement("div");
    $list.className = "phone-list";
    $list.addEventListener("click", onPhoneClick);
    document.querySelector(".index-page").append($list);
    phone.$list = $list;
  }

  renderPhone();
  $crumb.scrollLeft = $crumb.scrollWidth;
}

function unmountPhoneLayout() {
  closePhoneMenu();
  for (const { $node, $parent, $next } of phone.borrowed.reverse()) {
    $parent.insertBefore($node, $next);
  }
  phone.borrowed = [];
  document.getElementById("search")?.removeAttribute("placeholder");
  phone.$top.remove();
  phone.$top = null;
  phone.$list?.remove();
  phone.$list = null;
  document.documentElement.removeAttribute(PHONE_ATTRIBUTE);
}

function borrowNode($node, $into, $before = null) {
  phone.borrowed.push({ $node, $parent: $node.parentNode, $next: $node.nextSibling });
  $into.insertBefore($node, $before);
}

function renderPhone() {
  if (!phone.$top) return;
  renderPhoneHeader();
  const isSearchShown = phone.isSearchOpen && isSearchAvailable();
  phone.$top.querySelector(".phone-crumb").classList.toggle("hidden", isSearchShown);
  phone.$top.querySelector(".phone-search").classList.toggle("hidden", !isSearchShown);
  phone.$top.querySelector(".phone-sort").classList.toggle("hidden", DATA.kind !== "Index");
  if (phone.$list) renderPhoneRows();
}

function renderPhoneHeader() {
  if (!phone.$top) return;
  const $hdr = phone.$top.querySelector(".phone-hdr");
  const up = parentFolderUrl();
  const buttons = [];
  if (DATA.kind === "Index") {
    if (isSearchAvailable()) {
      buttons.push(`<button type="button" class="phone-btn ${phone.isSearchOpen ? "on" : ""}" data-phone-act="search" aria-label="Search">${PHONE_ICONS.search}</button>`);
    }
    buttons.push(`<button type="button" class="phone-btn" data-phone-act="folder-menu" aria-label="More">${PHONE_ICONS.kebab}</button>`);
  } else {
    if (phone.savedText !== null) {
      buttons.push(`<button type="button" class="phone-save" data-phone-act="save">Save</button>`);
    }
    buttons.push(`<button type="button" class="phone-btn" data-phone-act="file-menu" aria-label="More">${PHONE_ICONS.kebab}</button>`);
  }
  $hdr.innerHTML = `
    <button type="button" class="phone-btn" data-phone-act="up" aria-label="Up one folder" ${up === null ? "disabled" : ""}>${PHONE_ICONS.up}</button>
    <span class="phone-title"></span>
    ${buttons.join("")}`;
  updateSaveButton();
  $hdr.querySelector(".phone-title").textContent = folderTitle(DATA.href);
}

function updateSaveButton() {
  phone.$top?.querySelector(".phone-save")?.toggleAttribute("disabled", $editor.value === phone.savedText);
}

// The sort keys are the table's own header links, so sorting keeps dufs's rule (a new key
// sorts descending, the current one flips) and its server-side order.
function renderSortKeys($sort) {
  for (const key of ["name", "mtime", "size"]) {
    const $link = $pathsTableHead.querySelector(`th.cell-${key} a`);
    if (!$link) continue;
    const $key = $link.cloneNode(true);
    $key.className = "phone-sort-key";
    $key.classList.toggle("on", PARAMS.sort === key);
    $sort.append($key);
  }
}

// One row per entry the table would show, addressed by its DATA.paths index as the table's
// action cells are.
function renderPhoneRows() {
  const showHidden = isShowingHiddenFiles();
  const $rows = [];
  DATA.paths.forEach((file, index) => {
    if (!file) return;
    if (!showHidden && isHiddenPathName(file.name)) return;
    $rows.push(phoneRow(file, index));
  });
  phone.$list.replaceChildren(...$rows);
}

function phoneRow(file, index) {
  const isDir = file.path_type.endsWith("Dir");
  // A search result's name is its path relative to the folder searched; the row shows the
  // entry's own name with the folder it sits in beneath.
  const segments = file.name.split("/");
  const name = segments[segments.length - 1];

  const $row = document.createElement("div");
  $row.className = "phone-row";
  $row.dataset.phoneIndex = String(index);
  $row.innerHTML = `
    <a class="phone-row-link">
      <span class="phone-row-icon">${getPathSvg(file.path_type)}</span>
      <span class="phone-row-text"><span class="phone-row-name"></span><span class="phone-row-sub"></span></span>
    </a>
    <button type="button" class="phone-btn phone-row-more" data-phone-act="row-menu">${PHONE_ICONS.kebab}</button>`;
  $row.querySelector(".phone-row-link").href = isDir ? newUrl(file.name) + "/" : fileEditorUrl(file.name);
  $row.querySelector(".phone-row-name").textContent = name;
  const $sub = $row.querySelector(".phone-row-sub");
  if (segments.length > 1) {
    $sub.textContent = segments.slice(0, -1).join("/");
  } else {
    const size = isDir ? formatDirSize(file.size).trim() : formatFileSize(file.size).join(" ");
    $sub.innerHTML = `<span></span><span class="phone-row-dot">·</span><span></span>`;
    $sub.firstElementChild.textContent = formatMtime(file.mtime);
    $sub.lastElementChild.textContent = size;
  }
  $row.querySelector(".phone-row-more").setAttribute("aria-label", `Actions for ${name}`);
  return $row;
}

// The table opens a file for editing where it offers the Edit action, and read-only otherwise.
function canEditFiles() {
  return DATA.allow_delete && DATA.allow_upload;
}

function fileEditorUrl(name) {
  return newUrl(name) + (canEditFiles() ? "?edit" : "?view");
}

// The folder one hop up, linked as dufs's breadcrumb links it; null at the served root.
function parentFolderUrl() {
  const parts = DATA.href.split("/").filter((part) => part !== "");
  if (parts.length === 0) return null;
  const parent = parts.slice(0, -1).map(encodeURIComponent).join("/");
  const prefix = DATA.uri_prefix.endsWith("/") ? DATA.uri_prefix : DATA.uri_prefix + "/";
  return parent === "" ? prefix : `${prefix}${parent}/`;
}

function isSearchAvailable() {
  return phone.borrowed.some(({ $node }) => $node.classList.contains("searchbar"));
}

function onPhoneClick(event) {
  const $target = event.target.closest("[data-phone-act]");
  if (!$target) return;
  switch ($target.dataset.phoneAct) {
    case "up": {
      const up = parentFolderUrl();
      if (up !== null) location.href = up;
      break;
    }
    case "search":
      if (phone.isSearchOpen) {
        closeSearch();
      } else {
        phone.isSearchOpen = true;
        renderPhone();
        document.getElementById("search").focus();
      }
      break;
    case "search-close":
      closeSearch();
      break;
    case "folder-menu":
      openPhoneMenu($target, "top", null, folderActions());
      break;
    case "file-menu":
      openPhoneMenu($target, "sheet", menuTitle(folderTitle(DATA.href), "File"), fileActions());
      break;
    case "row-menu": {
      const index = Number($target.closest(".phone-row").dataset.phoneIndex);
      const file = DATA.paths[index];
      if (!file) break;
      openPhoneMenu($target, "sheet", menuTitle(file.name.split("/").pop(), file.path_type), entryActions(file, index));
      break;
    }
    case "save":
      saveChange();
      break;
  }
}

// Closing search on a results page leaves the results for the folder they were searched in.
function closeSearch() {
  if (PARAMS.q) {
    location.href = baseUrl();
    return;
  }
  phone.isSearchOpen = false;
  renderPhone();
}

// A toolbox control dufs enabled for this page, or null.
function toolboxControl(selector) {
  const $control = document.querySelector(`.head ${selector}`);
  return $control && !$control.classList.contains("hidden") ? $control : null;
}

function controlIcon($control, selector = "svg") {
  const $svg = $control.querySelector(selector).cloneNode(true);
  $svg.removeAttribute("class");
  return $svg.outerHTML;
}

// The folder's toolbox verbs, each the toolbox control's own handler.
function folderActions() {
  const actions = [];
  const $toggle = toolboxControl(".toggle-hidden-files");
  if ($toggle) {
    const isShowing = isShowingHiddenFiles();
    actions.push({
      label: isShowing ? "Hide system files" : "Show system files",
      icon: controlIcon($toggle, isShowing ? ".icon-eye-slash" : ".icon-eye"),
      run: () => {
        $toggle.click();
        renderPhone();
      },
    });
  }
  const $download = toolboxControl(".download");
  if ($download) {
    actions.push({ label: "Download folder as .zip", icon: ICONS.download, run: () => $download.click() });
  }
  const creates = [];
  const $upload = toolboxControl(".upload-file");
  if ($upload) {
    creates.push({ label: "Upload files or folders", icon: controlIcon($upload), run: () => document.getElementById("file").click() });
  }
  const $newFolder = toolboxControl(".new-folder");
  if ($newFolder) {
    creates.push({ label: "New folder", icon: controlIcon($newFolder), run: () => $newFolder.click() });
  }
  const $newFile = toolboxControl(".new-file");
  if ($newFile) {
    creates.push({ label: "New file", icon: controlIcon($newFile), run: () => $newFile.click() });
  }
  if (actions.length > 0 && creates.length > 0) actions.push("separator");
  return [...actions, ...creates];
}

// An entry's actions, offered and performed as its row in the table offers them.
function entryActions(file, index) {
  const isDir = file.path_type.endsWith("Dir");
  const actions = [];
  const $download = document.querySelector(`#addPath${index} a.dlwt`);
  if ($download) {
    actions.push({ label: isDir ? "Download folder as .zip" : "Download file", icon: ICONS.download, run: () => $download.click() });
  }
  if (!isDir) {
    actions.push(canEditFiles()
      ? { label: "Edit file", icon: ICONS.edit, run: () => { location.href = fileEditorUrl(file.name); } }
      : { label: "View file", icon: ICONS.view, run: () => { location.href = fileEditorUrl(file.name); } });
  }
  if (canEditFiles()) {
    actions.push({ label: "Move & Rename", icon: ICONS.move, run: () => movePath(index) });
  }
  if (DATA.allow_delete) {
    actions.push({
      label: "Delete",
      icon: ICONS.delete,
      run: async () => {
        try {
          await deletePath(index);
        } finally {
          renderPhone();
        }
      },
    });
  }
  return actions;
}

// The editor page's toolbox verbs for the file it shows.
function fileActions() {
  const actions = [];
  const $download = toolboxControl(".download");
  if ($download) actions.push({ label: "Download file", icon: ICONS.download, run: () => $download.click() });
  const $move = toolboxControl(".move-file");
  if ($move) actions.push({ label: "Move & Rename", icon: ICONS.move, run: () => $move.click() });
  const $delete = toolboxControl(".delete-file");
  if ($delete) actions.push({ label: "Delete", icon: ICONS.delete, run: () => $delete.click() });
  return actions;
}

function menuTitle(name, pathType) {
  return { name, icon: getPathSvg(pathType) };
}

// A menu over the page: "top" drops from the header, "sheet" rises from the bottom over a dim
// scrim. Tapping the scrim, pressing Escape, or choosing a row closes it.
function openPhoneMenu($trigger, kind, title, actions) {
  closePhoneMenu();
  if (actions.length === 0) return;

  const $layer = document.createElement("div");
  $layer.className = "phone-menu-layer";
  const $scrim = document.createElement("div");
  $scrim.className = kind === "sheet" ? "phone-scrim dim" : "phone-scrim";
  $scrim.addEventListener("click", closePhoneMenu);
  const $menu = document.createElement("div");
  $menu.className = `phone-menu phone-menu-${kind}`;
  $menu.setAttribute("role", "menu");
  if (title) {
    const $title = document.createElement("div");
    $title.className = "phone-menu-title";
    $title.innerHTML = `<span class="phone-row-icon">${title.icon}</span><span></span>`;
    $title.lastElementChild.textContent = title.name;
    $menu.append($title);
  }
  for (const action of actions) {
    if (action === "separator") {
      const $separator = document.createElement("div");
      $separator.className = "phone-menu-sep";
      $menu.append($separator);
      continue;
    }
    const $item = document.createElement("button");
    $item.type = "button";
    $item.className = "phone-menu-item";
    $item.setAttribute("role", "menuitem");
    $item.innerHTML = `<span class="phone-menu-glyph">${action.icon}</span><span></span>`;
    $item.lastElementChild.textContent = action.label;
    $item.addEventListener("click", () => {
      closePhoneMenu();
      action.run();
    });
    $menu.append($item);
  }
  $layer.append($scrim, $menu);
  document.body.append($layer);
  phone.$menuLayer = $layer;
  phone.$menuTrigger = $trigger;
  $trigger.classList.add("on");
}

function closePhoneMenu() {
  phone.$menuLayer?.remove();
  phone.$menuLayer = null;
  phone.$menuTrigger?.classList.remove("on");
  phone.$menuTrigger = null;
}
