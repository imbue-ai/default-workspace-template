/**
 * The names the user has given the machine's objects, keyed by ref.
 *
 * A rename names the **object**, machine-wide, not the tab showing it. There is
 * one live page per object (see views/liveSurfaces) and a project is only a
 * view that may or may not show it, so a name kept with a panel would be a name
 * kept in one view's saved layout: the app renamed "Docs" in one project would
 * still read "docs-viewer" in another, and an object with no panel anywhere --
 * backgrounded, still running, just not docked -- would have nowhere to keep a
 * name at all. Keying by ref makes both of those go away, which is why every
 * surface that names an object (the dock tab, the rail's tab list and its app
 * shortcuts, the All apps popover, the New Tab launcher, the project settings
 * list, and Everything) reads its label through here.
 *
 * This is a sibling of Projects rather than another export inside it, mirroring
 * the backend's split: a title belongs to the machine and a member list belongs
 * to a project, so ``member_titles.py`` sits beside ``projects.py`` and nothing
 * in projects reaches back into it. The dependency runs one way here too --
 * nothing below imports Projects -- so the store a name lives in stays
 * independent of the store a membership lives in.
 *
 * The map is cached the way the projects list is: fetched once at startup and
 * kept current by the ``member_title_changed`` broadcast, which every client
 * gets whether or not it is showing the object. A ref that is absent is simply
 * unnamed, and the caller falls back to whatever the object calls itself.
 */

import m from "mithril";

import { apiUrl } from "../base-path";

/** Every name the user has given an object, keyed by ref. */
export type MemberTitleMap = Readonly<Record<string, string>>;

// What the server last told us. Replaced wholesale by a load and patched by
// each broadcast; read synchronously by every view that draws a name.
let titleByRef: Record<string, string> = {};

/**
 * The display name for one object: its chosen name when it has one, else the
 * name derived from what the object is.
 *
 * ``legacyPanelTitle`` is a name found on a panel saved before titles were
 * keyed by ref (``panelParams.customTitle``). It is read as a fallback so a
 * layout saved with one does not lose its name, and it is only ever a fallback:
 * the store is where renames go now, so a chosen name always wins over the
 * stale copy a saved layout is still carrying.
 */
export function displayNameForRef(
  titles: MemberTitleMap,
  ref: string,
  derivedName: string,
  legacyPanelTitle?: string,
): string {
  const chosen = titles[ref];
  if (chosen !== undefined && chosen !== "") return chosen;
  if (legacyPanelTitle !== undefined && legacyPanelTitle !== "") return legacyPanelTitle;
  return derivedName;
}

/** The cached map, for callers that resolve several names at once. */
export function getMemberTitles(): MemberTitleMap {
  return titleByRef;
}

/** The chosen name for one object, or null when it has none. */
export function getMemberTitle(ref: string): string | null {
  const chosen = titleByRef[ref];
  return chosen === undefined || chosen === "" ? null : chosen;
}

/** ``displayNameForRef`` against the cached map: what a surface draws for one
 *  object right now. */
export function displayNameForMember(ref: string, derivedName: string, legacyPanelTitle?: string): string {
  return displayNameForRef(titleByRef, ref, derivedName, legacyPanelTitle);
}

/** Fetch the whole machine-wide map. Defensive like fetchProjectsList: an
 *  unreachable server yields an empty map, which reads as "nothing has been
 *  renamed" rather than breaking every surface that draws a name. */
export async function fetchMemberTitles(): Promise<Record<string, string>> {
  try {
    const response = await fetch(apiUrl("/api/member-titles"));
    if (!response.ok) return {};
    const data = (await response.json()) as { titles?: Record<string, string> };
    return data.titles ?? {};
  } catch {
    return {};
  }
}

/** Load the map into the cache. Called once at startup, before the first view
 *  is mounted, so the first paint already says what things are called. */
export async function loadMemberTitles(): Promise<void> {
  titleByRef = await fetchMemberTitles();
}

/** Record what the server says one object is called now; null means it is
 *  unnamed again. The `member_title_changed` broadcast lands here, and so does
 *  the response to our own rename. */
export function applyMemberTitleChange(ref: string, title: string | null): void {
  if (title === null || title === "") {
    delete titleByRef[ref];
    return;
  }
  titleByRef[ref] = title;
}

async function errorDetailFromResponse(response: Response): Promise<string> {
  const data = (await response.json().catch(() => ({}))) as { detail?: string };
  return data.detail ?? `HTTP ${response.status}`;
}

/**
 * Name one object, machine-wide, or clear its name with a blank one.
 *
 * The ref does not have to be filed anywhere and does not have to have a panel:
 * an object in no project at all still shows in Everything, and a backgrounded
 * member is renameable with no tab to hang the name on -- which is the point of
 * keying this by ref. The cache is updated from the server's own answer (it
 * trims), and the broadcast that follows repaints every other client. Throws
 * with the server's detail on rejection (a bad ref, a name over the cap).
 *
 * The new name is shown BEFORE the server has agreed to it, and put back if it
 * refuses. Renaming a chat goes all the way out to the ``mngr`` CLI, whose
 * startup alone is several seconds, so waiting for the round trip meant typing
 * a name and watching the old one sit there -- with no indication anything had
 * happened. What the user typed is what they meant; the server's answer is a
 * correction, not the source of truth for what to paint.
 *
 * Reverting restores whatever the name was when this call started. Two renames
 * of the SAME ref overlapping would therefore have the loser restore a stale
 * name, which is the honest outcome available here: this store holds one name
 * per ref, so there is nothing finer to roll back to. The next broadcast
 * settles every client regardless.
 */
export async function setMemberTitle(ref: string, title: string): Promise<string | null> {
  const previousTitle = titleByRef[ref] ?? null;
  applyMemberTitleChange(ref, title.trim());
  m.redraw();
  let response: Response;
  try {
    response = await fetch(apiUrl("/api/member-titles"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref, title }),
    });
  } catch (e) {
    applyMemberTitleChange(ref, previousTitle);
    m.redraw();
    throw e;
  }
  if (!response.ok) {
    const detail = await errorDetailFromResponse(response);
    applyMemberTitleChange(ref, previousTitle);
    m.redraw();
    throw new Error(detail);
  }
  const data = (await response.json()) as { title?: string | null };
  const stored = data.title ?? null;
  applyMemberTitleChange(ref, stored);
  m.redraw();
  return stored;
}

/**
 * Carry a name across a change of identity, when the object stays the same one.
 *
 * A terminal is filed under its tmux session name, so renaming the session
 * moves the ref the object answers to. The name the user chose belongs to the
 * object rather than to the session it happens to be attached to, so it moves
 * with it; an object that was never named moves nothing and writes nothing.
 */
export async function moveMemberTitle(fromRef: string, toRef: string): Promise<void> {
  if (fromRef === toRef) return;
  const chosen = getMemberTitle(fromRef);
  if (chosen === null) return;
  await setMemberTitle(toRef, chosen);
  await setMemberTitle(fromRef, "");
}
