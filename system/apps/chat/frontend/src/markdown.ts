import m from "mithril";
import DOMPurify from "dompurify";
import { Marked } from "marked";
import { applyPartAttrs } from "@imbue/workspace-ui/src/themes/parts";
import { classifyLink, fileUrl } from "@imbue/workspace-ui/src/links";
import { openImageLightbox } from "./lightbox";
import { localImageUrl, uploadedImageSize } from "./models/attachments";
import { isBlockExpanded, setBlockExpanded } from "./views/expansion-state";
import { TOOL_CALL_PART } from "./views/ToolCallBlock";

const marked = new Marked({
  breaks: true,
  gfm: true,
});

const TOOL_CALL_PREFIX = "Tool call: ";

// DOMPurify's own default (3.x ``IS_ALLOWED_URI``) plus ``file:`` and every other scheme but the script-running
// ``javascript:``, ``vbscript:`` and ``data:``, so a message's ``file:`` or custom-scheme (``slack:``) link reaches
// ``rewritePathLinks`` rather than losing its target.
const ALLOWED_URI_REGEXP =
  /^(?:(?:(?:f|ht)tps?|file|mailto|tel|callto|sms|cid|xmpp|matrix):|[^a-z]|[a-z+.-]+(?:[^a-z+.:-]|$)|(?!(?:javascript|vbscript|data):)[a-z][a-z0-9+.-]*:)/i;

export function renderMarkdown(source: string): string {
  const rawHtml = marked.parse(source) as string;
  const fragment = DOMPurify.sanitize(rawHtml, { RETURN_DOM_FRAGMENT: true, ALLOWED_URI_REGEXP });
  rewritePathLinks(fragment);
  // The fragment belongs to DOMPurify's inert document. Serializing it through a live-document
  // element would adopt its <img>s into the page, and an adopted image starts fetching.
  const container = fragment.ownerDocument.createElement("div");
  container.append(fragment);
  return container.innerHTML;
}

/**
 * Give each message link the target the workspace opens it at, and keep a click on it from replacing the conversation.
 *
 * An absolute path names a file of the workspace (see the show-files-in-chat skill), so it becomes that file's
 * ``file:`` URL, which the app contract hands to the shell to open in Files, as it does a local URL; every
 * other link keeps its real target, so hovering it and copying its address show where it goes, and opens in a new
 * browsing context should nothing route it (a chat opened on its own). Anything else (a relative path, a fragment,
 * a file and line, a host and port with no scheme, another of the browser's own schemes) cannot open anything, so it
 * is unwrapped to its text.
 */
function rewritePathLinks(root: DocumentFragment): void {
  for (const anchor of Array.from(root.querySelectorAll("a"))) {
    const target = classifyLink(messageLinkUrl(anchor.getAttribute("href") ?? ""), window.location.host);
    anchor.removeAttribute("download");
    if (target.kind === "unroutable") {
      anchor.replaceWith(...Array.from(anchor.childNodes));
    } else if (target.kind === "file") {
      anchor.setAttribute("href", fileUrl(target.path));
      anchor.removeAttribute("target");
    } else {
      anchor.setAttribute("target", "_blank");
      anchor.setAttribute("rel", "noopener noreferrer");
    }
  }
}

/** A message link's target as a URL: an absolute path becomes its ``file:`` URL, without the query or fragment (the
 *  chat's own cache key among them); anything else, ``//host`` and ``/\host`` included (another host to a browser,
 *  not a path, even with a tab or newline inside, which a URL parser drops), is left as written. */
function messageLinkUrl(href: string): string {
  const parsed = href.replace(/[\t\n\r]/g, "");
  if (!/^\/(?![/\\])/.test(parsed)) return href;
  const url = new URL(parsed, "file:///");
  url.search = "";
  url.hash = "";
  return url.href;
}

/**
 * Append a per-message ``requested_at`` query parameter to chat image URLs.
 *
 * Chat markdown references an image by its absolute on-disk path, which the
 * backend serves with a one-year ``immutable`` cache policy. If an agent
 * overwrites a previously referenced file, a *new* message reusing that path
 * would otherwise render the browser's stale cached copy. Tagging each message's
 * image ``src`` with the message's post time makes its URL
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

const IMAGE_LOADING_CLASS = "is-loading";
const IMAGE_ARRIVING_CLASS = "is-arriving";

/** Give a loaded image that has no width and height of its own the size the browser decoded. An SVG
 *  with only a viewBox has none, and in a bubble that shrinks to fit its content it would draw at nothing. */
function holdDecodedSize(image: HTMLImageElement): void {
  if (image.hasAttribute("width") || image.naturalWidth === 0) return;
  image.setAttribute("width", String(image.naturalWidth));
  image.setAttribute("height", String(image.naturalHeight));
}

function revealLoadedImage(event: Event): void {
  const image = event.target as HTMLImageElement;
  holdDecodedSize(image);
  image.classList.remove(IMAGE_LOADING_CLASS);
  image.classList.add(IMAGE_ARRIVING_CLASS);
  // With reduced motion there is no fade, and so no end of one to wait for.
  if (getComputedStyle(image).animationName === "none") {
    image.classList.remove(IMAGE_ARRIVING_CLASS);
    return;
  }
  image.addEventListener("animationend", () => image.classList.remove(IMAGE_ARRIVING_CLASS), { once: true });
}

function clearImageLoading(event: Event): void {
  (event.target as HTMLElement).classList.remove(IMAGE_LOADING_CLASS);
}

/**
 * Point each image at what draws it soonest and keep its space while it loads. An upload's path carries
 * the size it is drawn at, so its box takes exactly that space; an image this document uploaded draws
 * from its local copy; any other is tagged with ``requestedAt``. Until it loads, an image holds a
 * placeholder box (at a fixed shape when its size is unknown), so a message that is only an image never
 * shows as an empty bubble, then fades in over that box.
 */
function prepareImages(container: HTMLElement, requestedAt: string | undefined): void {
  for (const image of Array.from(container.querySelectorAll("img"))) {
    const src = image.getAttribute("src") ?? "";
    const size = uploadedImageSize(src);
    if (size !== null) {
      image.setAttribute("width", String(size.width));
      image.setAttribute("height", String(size.height));
    }
    const localUrl = localImageUrl(src);
    if (localUrl !== undefined) {
      image.setAttribute("src", localUrl);
    } else if (requestedAt !== undefined) {
      const tagged = requestedAtUrl(src, requestedAt);
      if (tagged !== null) image.setAttribute("src", tagged);
    }
    if (image.complete) {
      holdDecodedSize(image);
      continue;
    }
    image.classList.add(IMAGE_LOADING_CLASS);
    image.addEventListener("load", revealLoadedImage, { once: true });
    image.addEventListener("error", clearImageLoading, { once: true });
  }
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
    applyPartAttrs(wrapper, TOOL_CALL_PART);
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
    prepareImages(element, vnode.attrs.requestedAt || undefined);
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
    prepareImages(element, vnode.attrs.requestedAt || undefined);
    restoreExpandedState(element, expanded);
  },
  view() {
    return m("div", { class: "message-content markdown-content" });
  },
};
