/**
 * Paths as the Files app handles them: absolute, decoded, with a folder's path ending in `/` (`/home/user/`) and a
 * file's not (`/home/user/notes.md`). dufs serves the filesystem root, so a path is also the URL path of its page,
 * once encoded.
 */

export const ROOT = "/";

export function isFolderPath(path: string): boolean {
  return path.endsWith("/");
}

/** The names along a path: `/a/b/` and `/a/b` are both `["a", "b"]`. */
export function segmentsOf(path: string): string[] {
  return path.split("/").filter((segment) => segment !== "");
}

export function folderPathOf(segments: readonly string[]): string {
  return segments.length === 0 ? ROOT : `/${segments.join("/")}/`;
}

/** The path of `name` inside `folder`, a folder's path when `isFolder`. */
export function childPath(folder: string, name: string, isFolder: boolean): string {
  return `${folder}${name}${isFolder ? "/" : ""}`;
}

/** The folder holding `path`; null for the root. */
export function parentOf(path: string): string | null {
  const segments = segmentsOf(path);
  if (segments.length === 0) return null;
  return folderPathOf(segments.slice(0, -1));
}

/** The last name of a path; empty for the root. */
export function nameOf(path: string): string {
  const segments = segmentsOf(path);
  return segments.length === 0 ? "" : segments[segments.length - 1];
}

/** Every folder from the root down to `folder`, itself included. */
export function ancestorsOf(folder: string): string[] {
  const segments = segmentsOf(folder);
  return [ROOT, ...segments.map((_, index) => folderPathOf(segments.slice(0, index + 1)))];
}

/** Whether `path` is `folder` or lies somewhere under it. */
export function isWithin(path: string, folder: string): boolean {
  return path === folder || (isFolderPath(folder) && path.startsWith(folder));
}

/** The lower-cased extension after the last dot, or "" (a leading dot names a hidden file, not an extension). */
export function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  if (dot <= 0 || dot === name.length - 1) return "";
  return name.slice(dot + 1).toLowerCase();
}

/** How much of a name a rename selects first: everything before the extension, as Finder does. */
export function stemLength(name: string): number {
  const extension = extensionOf(name);
  return extension === "" ? name.length : name.length - extension.length - 1;
}

/** Whether a name is one the app hides unless asked: a dotted "system file". A search result's name is a path
 *  below the folder searched, hidden when any of its segments is. */
export function isHiddenName(name: string): boolean {
  return name.split("/").some((segment) => segment.startsWith("."));
}

/** Why a name cannot be given to an entry, or null when it can. */
export function invalidNameReason(name: string): string | null {
  if (name.trim() === "") return "A name can't be empty.";
  if (name.includes("/")) return 'A name can\'t contain "/".';
  if (name === "." || name === "..") return `"${name}" is reserved.`;
  return null;
}

/** `name`, or the first of `name 2`, `name 3`, ... (before the extension) that `isTaken` does not claim. */
export function uniqueName(name: string, isTaken: (candidate: string) => boolean): string {
  if (!isTaken(name)) return name;
  const stem = name.slice(0, stemLength(name));
  const rest = name.slice(stem.length);
  for (let counter = 2; ; counter += 1) {
    const candidate = `${stem} ${counter}${rest}`;
    if (!isTaken(candidate)) return candidate;
  }
}

/** A path as it goes in a URL: each name percent-encoded, the slashes between them kept. */
export function encodePath(path: string): string {
  return path
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");
}

/** A URL's pathname as a path. A malformed escape is left as it came. */
export function decodePathname(pathname: string): string {
  return pathname
    .split("/")
    .map((segment) => {
      try {
        return decodeURIComponent(segment);
      } catch {
        return segment;
      }
    })
    .join("/");
}

/** What a window showing `path` is called: the last name, or "Files" at the root. */
export function titleOf(path: string): string {
  const name = nameOf(path);
  return name === "" ? "Files" : name;
}
