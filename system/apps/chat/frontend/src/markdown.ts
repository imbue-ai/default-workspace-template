import m from "mithril";
import DOMPurify from "dompurify";
import { Marked } from "marked";
import { openImageLightbox } from "./lightbox";
import { isBlockExpanded, setBlockExpanded } from "./views/expansion-state";

const marked = new Marked({
  breaks: true,
  gfm: true,
});

const TOOL_CALL_PREFIX = "Tool call: ";

export function renderMarkdown(source: string): string {
  const rawHtml = marked.parse(source) as string;
  const fragment = DOMPurify.sanitize(rawHtml, { RETURN_DOM_FRAGMENT: true });
  rewritePathLinks(fragment);
  // The fragment belongs to DOMPurify's inert document. Serializing it through a live-document
  // element would adopt its <img>s into the page, and an adopted image starts fetching.
  const container = fragment.ownerDocument.createElement("div");
  container.append(fragment);
  return container.innerHTML;
}

/**
 * Keep a clicked message link from replacing the conversation.
 *
 * A web link is left alone: the desktop app opens external links in the browser.
 * An absolute path is a file the chat backend serves as a download (see the
 * show-files-in-chat skill); ``download`` makes a missing file fail as a download
 * rather than load an error page over the chat. Any other path (relative, a
 * fragment, ``file:``) cannot open anything from the chat's origin, so it is
 * unwrapped to its text.
 * A dotted "scheme" is a file name with a line number (``q4.md:12``), not a URL.
 */
function rewritePathLinks(root: DocumentFragment): void {
  for (const anchor of Array.from(root.querySelectorAll("a"))) {
    const href = anchor.getAttribute("href") ?? "";
    const isWebLink = /^[a-z][a-z\d+-]*:/i.test(href) && !/^file:/i.test(href);
    if (isWebLink || href.startsWith("//")) continue;
    if (href.startsWith("/")) {
      anchor.setAttribute("download", "");
      continue;
    }
    anchor.replaceWith(...Array.from(anchor.childNodes));
  }
}

/**
 * Append a per-message ``requested_at`` query parameter to chat file URLs.
 *
 * Chat markdown references a file by its absolute on-disk path, which the
 * backend serves with a one-year ``immutable`` cache policy. If an agent
 * overwrites a previously referenced file, a *new* message reusing that path
 * would otherwise render the browser's stale cached copy. Tagging each message's
 * image ``src`` and link ``href`` with the message's post time makes its URL
 * unique per message: a new message's URL has never been cached, so the browser
 * fetches the file's current bytes, while re-rendering the *same* message reuses
 * its stable URL (and the immutable cache) without refetching. The parameter is
 * a pure cache key -- the server ignores it and serves the file as usual.
 *
 * External URLs (``https://``, ``//``), app routes (``/api/...``), and paths
 * that already carry a query string are left untouched.
 */
export function requestedAtUrl(value: string, requestedAt: string): string | null {
  if (!value.startsWith("/") || value.startsWith("//") || value.startsWith("/api/")) return null;
  if (value.includes("?")) return null;
  return `${value}?requested_at=${encodeURIComponent(requestedAt)}`;
}

function appendRequestedAt(container: HTMLElement, requestedAt: string): void {
  const tagAttribute = (element: Element, attribute: string): void => {
    const tagged = requestedAtUrl(element.getAttribute(attribute) ?? "", requestedAt);
    if (tagged !== null) element.setAttribute(attribute, tagged);
  };
  for (const image of Array.from(container.querySelectorAll("img"))) tagAttribute(image, "src");
  for (const anchor of Array.from(container.querySelectorAll("a"))) tagAttribute(anchor, "href");
}

function hasToolCallLine(textContent: string): boolean {
  for (const line of textContent.split("\n")) {
    if (line.trim().startsWith(TOOL_CALL_PREFIX)) {
      return true;
    }
  }
  return false;
}

/*
 * On the backend, we use the --td argument to llm and include
 * the debug output in the stream. For each tool call, the debug
 * output starts with a line that starts with "Tool call: ".
 */
function wrapToolCallBlocks(container: HTMLElement, expansionKeyPrefix: string | undefined): void {
  let blockIndex = 0;
  for (const preElement of Array.from(container.querySelectorAll("pre"))) {
    if (preElement.parentElement?.classList.contains("tool-call-block")) {
      continue;
    }
    const codeElement = preElement.querySelector("code");
    if (codeElement === null) {
      continue;
    }
    if (!hasToolCallLine(codeElement.textContent ?? "")) {
      continue;
    }

    codeElement.textContent = (codeElement.textContent ?? "").replace(/^\s*\n/, "");

    const wrapper = document.createElement("div");
    wrapper.className = "tool-call-block";
    // Keyed store keeps the expansion across full innerHTML rewrites and row
    // remounts; the index within the message is stable because the source
    // markdown is. Without a prefix (no stable message identity) the DOM
    // toggle still works, it just cannot survive a remount.
    const expansionKey = expansionKeyPrefix !== undefined ? `md:${expansionKeyPrefix}#${blockIndex}` : null;
    blockIndex += 1;
    if (expansionKey !== null && isBlockExpanded(expansionKey)) {
      wrapper.classList.add("tool-call-block--expanded");
    }
    wrapper.addEventListener("click", () => {
      const isNowExpanded = wrapper.classList.toggle("tool-call-block--expanded");
      if (expansionKey !== null) {
        setBlockExpanded(expansionKey, isNowExpanded);
      }
    });

    preElement.replaceWith(wrapper);
    wrapper.appendChild(preElement);
  }
}

function saveExpandedState(container: HTMLElement): Set<number> {
  const expanded = new Set<number>();
  const blocks = container.querySelectorAll(".tool-call-block");
  blocks.forEach((block, index) => {
    if (block.classList.contains("tool-call-block--expanded")) {
      expanded.add(index);
    }
  });
  return expanded;
}

function restoreExpandedState(container: HTMLElement, expanded: Set<number>): void {
  const blocks = container.querySelectorAll(".tool-call-block");
  blocks.forEach((block, index) => {
    if (expanded.has(index)) {
      block.classList.add("tool-call-block--expanded");
    }
  });
}

function handleMarkdownImageClick(event: MouseEvent): void {
  const target = event.target;
  if (target instanceof HTMLImageElement) {
    event.preventDefault();
    openImageLightbox(target.src, target.alt);
  }
}

export const MarkdownContent: m.Component<{ content: string; requestedAt?: string; expansionKeyPrefix?: string }> = {
  oncreate(vnode) {
    const element = vnode.dom as HTMLElement;
    element.innerHTML = renderMarkdown(vnode.attrs.content);
    wrapToolCallBlocks(element, vnode.attrs.expansionKeyPrefix);
    if (vnode.attrs.requestedAt) {
      appendRequestedAt(element, vnode.attrs.requestedAt);
    }
    // Clicking an inline image opens it full-screen. The listener is delegated
    // on the container, which mithril reuses across redraws, so it survives the
    // innerHTML resets in onupdate without needing to be re-attached.
    element.addEventListener("click", handleMarkdownImageClick);
  },
  // Skip the subtree diff (and the onupdate innerHTML rewrite below) whenever the
  // markdown source is unchanged. onupdate re-sets innerHTML, which destroys every
  // text node in the subtree; the browser then collapses any selection anchored in
  // it. Since a global redraw fires on every scroll tick and every streamed event,
  // an unguarded rewrite kills a user's text selection on the first frame. The
  // rendered element is a childless div whose content we manage by hand, so there is
  // nothing for Mithril to diff -- retaining the DOM untouched is always correct when
  // the content matches.
  onbeforeupdate(vnode, old) {
    return vnode.attrs.content !== old.attrs.content;
  },
  onupdate(vnode) {
    const element = vnode.dom as HTMLElement;
    const expanded = saveExpandedState(element);
    element.innerHTML = renderMarkdown(vnode.attrs.content);
    wrapToolCallBlocks(element, vnode.attrs.expansionKeyPrefix);
    if (vnode.attrs.requestedAt) {
      appendRequestedAt(element, vnode.attrs.requestedAt);
    }
    restoreExpandedState(element, expanded);
  },
  view() {
    return m("div", { class: "message-content markdown-content" });
  },
};
