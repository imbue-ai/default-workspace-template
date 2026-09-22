/** Keep message links out of the conversation, routing workspace files to Files. */
import { connectToShell, type ShellConnection } from "@imbue/workspace-ui/src/app_contract";
import { deriveAppOrigin, workspaceHostCoordinate } from "@imbue/workspace-ui/src/origin";

let connection: ShellConnection | null = null;

export function workspaceFilePath(href: string, roots: readonly string[]): string | null {
  if (!href || href.startsWith("#") || href.startsWith("//") || /^[a-z][a-z\d+.-]*:/i.test(href)) return null;
  let path: string;
  try {
    path = decodeURIComponent(href.split(/[?#]/, 1)[0]);
  } catch {
    return null;
  }
  path = path.replace(/:\d+(?::\d+)?$/, "");
  const root = roots.find((candidate) => path === candidate || path.startsWith(candidate + "/"));
  if (root !== undefined) path = path.slice(root.length);
  else if (path.startsWith("/") && !/^\/(?:data|docs|system|apps|skills|\.agents)\//.test(path)) return null;
  const normalized = new URL(
    path.replace(/^\/+/, "").split("/").map(encodeURIComponent).join("/"),
    "https://workspace.invalid/",
  ).pathname;
  return normalized + (path.endsWith("/") ? "" : "?view");
}

export function prepareFileLinks(container: HTMLElement): void {
  const roots: string[] = JSON.parse(
    document.querySelector('meta[name="chat-workspace-roots"]')?.getAttribute("content") ?? "[]",
  );
  const label = document.querySelector('meta[name="chat-files-label"]')?.getAttribute("content");
  for (const anchor of container.querySelectorAll<HTMLAnchorElement>("a[href]")) {
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    const path = workspaceFilePath(anchor.getAttribute("href") ?? "", roots);
    if (path !== null) {
      anchor.dataset.workspaceFile = path;
      if (label && workspaceHostCoordinate(window.location.host) !== window.location.host) {
        anchor.href = new URL(path, deriveAppOrigin(label)).href;
      }
    }
  }
}

export function handleFileLinkClick(event: MouseEvent): void {
  if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey)
    return;
  const anchor =
    event.target instanceof Element ? event.target.closest<HTMLAnchorElement>("a[data-workspace-file]") : null;
  const path = anchor?.dataset.workspaceFile;
  if (!path) return;
  connection ??= connectToShell({});
  if (!connection.isFramed) return;
  event.preventDefault();
  connection.openAppPath("files", path, "focus");
}
