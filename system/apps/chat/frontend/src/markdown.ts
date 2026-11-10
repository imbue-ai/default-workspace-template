import m from "mithril";
import DOMPurify from "dompurify";
import { Marked } from "marked";
import { applyPartAttrs } from "@imbue/workspace-ui/src/themes/parts";
import { buttonClass, buttonPartAttrs } from "@imbue/workspace-ui/src/components/Button";
import { classifyLink, fileUrl } from "@imbue/workspace-ui/src/links";
import { openImageLightbox } from "./lightbox";
import { localImageUrl, uploadedImageSize } from "./models/attachments";
import { isBlockExpanded, setBlockExpanded } from "./views/expansion-state";
import { TOOL_CALL_PART } from "./views/ToolCallBlock";
import { REPLY_HREF_PREFIX, canSendReply, sendReplyFromMessage } from "./views/reply-senders";

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
  convertReplyLinks(fragment);
  rewritePathLinks(fragment);
  // The fragment belongs to DOMPurify's inert document. Serializing it through a live-document
  // element would adopt its <img>s into the page, and an adopted image starts fetching.
  const container = fragment.ownerDocument.createElement("div");
  container.append(fragment);
  return container.innerHTML;
}

/** The longest reply a button sends; a longer one is unwrapped to its text, as any other fragment link is. */
export const MAX_REPLY_TEXT_LENGTH = 200;

/** The attribute a reply button carries its reply in. */
export const REPLY_TEXT_ATTRIBUTE = "data-reply";

/** A reply button's own class: compact, inline beside its siblings, and wrapping a long label (style.css). */
export const REPLY_BUTTON_CLASS = "message-reply-button";

/** The reply a link's href offers, or null when the href is not a usable reply link: not exactly the
 *  ``#reply=`` form, not decodable, empty, longer than ``MAX_REPLY_TEXT_LENGTH``, or a slash command (which the
 *  composer's own guard would have to see first). */
export function replyTextOf(href: string): string | null {
  if (!href.startsWith(REPLY_HREF_PREFIX)) return null;
  let decoded: string;
  try {
    decoded = decodeURIComponent(href.slice(REPLY_HREF_PREFIX.length));
  } catch {
    return null;
  }
  const text = decoded.trim();
  if (text === "" || text.length > MAX_REPLY_TEXT_LENGTH || text.startsWith("/")) return null;
  return text;
}

/**
 * Turn each reply link into a button that sends its reply (see ``REPLY_HREF_PREFIX``), labelled with the link's
 * text. Runs on DOMPurify's output, so the button and its attributes are ours, and the reply is set as an attribute
 * value, never markup. A button starts disabled: ``MarkdownContent`` enables it when its chat has a composer to send
 * through and nothing has answered the message yet. A link that is not a usable reply link is left for
 * ``rewritePathLinks``, which unwraps a fragment link to its text.
 */
function convertReplyLinks(root: DocumentFragment): void {
  for (const anchor of Array.from(root.querySelectorAll("a"))) {
    const reply = replyTextOf(anchor.getAttribute("href") ?? "");
    if (reply === null) continue;
    const button = anchor.ownerDocument.createElement("button");
    button.type = "button";
    button.className = buttonClass("secondary", { sm: true, extra: REPLY_BUTTON_CLASS });
    applyPartAttrs(button, buttonPartAttrs("secondary", false));
    button.setAttribute(REPLY_TEXT_ATTRIBUTE, reply);
    button.disabled = true;
    button.textContent = anchor.textContent?.trim() || reply;
    anchor.replaceWith(button);
  }
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

/** How a message's reply buttons send: through the composer of ``chatId``, unless a user turn already answers the
 *  message. Without it (a read-only view, a message that is not the agent's) the buttons stay disabled. */
export interface MarkdownReplies {
  readonly chatId: string;
  readonly isAnswered: boolean;
}

export interface MarkdownContentAttrs {
  content: string;
  requestedAt?: string;
  expansionKeyPrefix?: string;
  replies?: MarkdownReplies;
}

// What each rendered container shows, read by the click handler (attached once, in oncreate) and by onupdate,
// which rewrites the markdown only when its source changed.
const renderedContentByElement = new WeakMap<Element, string>();
const repliesByElement = new WeakMap<Element, MarkdownReplies | undefined>();
// The reply state the container's buttons were last set from (see replyStateKey).
const replyStateByElement = new WeakMap<Element, string>();

function replyButtonsOf(container: Element): HTMLButtonElement[] {
  return Array.from(container.querySelectorAll<HTMLButtonElement>(`button[${REPLY_TEXT_ATTRIBUTE}]`));
}

/** Whether a message's reply buttons can be pressed now. */
function areRepliesOpen(replies: MarkdownReplies | undefined): boolean {
  return replies !== undefined && !replies.isAnswered && canSendReply(replies.chatId);
}

/** What the buttons' state is read from: the replies, and whether their chat has a composer to send through. */
function replyStateKey(replies: MarkdownReplies | undefined): string {
  return replies === undefined ? "" : `${replies.chatId}|${replies.isAnswered}|${canSendReply(replies.chatId)}`;
}

function syncReplyButtons(container: HTMLElement, replies: MarkdownReplies | undefined): void {
  repliesByElement.set(container, replies);
  replyStateByElement.set(container, replyStateKey(replies));
  const isOpen = areRepliesOpen(replies);
  for (const button of replyButtonsOf(container)) button.disabled = !isOpen;
}

/** A reply button's click sends its reply through the chat's composer, and disables every button of the message at
 *  once: the transcript marks the message answered only once the send shows, and a second click must not send twice. */
function handleReplyButtonClick(event: MouseEvent): void {
  const container = event.currentTarget as HTMLElement;
  const target = event.target;
  const button =
    target instanceof Element ? target.closest<HTMLButtonElement>(`button[${REPLY_TEXT_ATTRIBUTE}]`) : null;
  if (button === null || !container.contains(button) || button.disabled) return;
  event.preventDefault();
  const replies = repliesByElement.get(container);
  const reply = button.getAttribute(REPLY_TEXT_ATTRIBUTE) ?? "";
  for (const sibling of replyButtonsOf(container)) sibling.disabled = true;
  if (replies !== undefined && areRepliesOpen(replies)) sendReplyFromMessage(replies.chatId, reply);
}

function renderInto(element: HTMLElement, attrs: MarkdownContentAttrs): void {
  element.innerHTML = renderMarkdown(attrs.content);
  renderedContentByElement.set(element, attrs.content);
  wrapToolCallBlocks(element, attrs.expansionKeyPrefix);
  prepareImages(element, attrs.requestedAt || undefined);
  syncReplyButtons(element, attrs.replies);
}

export const MarkdownContent: m.Component<MarkdownContentAttrs> = {
  oncreate(vnode) {
    const element = vnode.dom as HTMLElement;
    renderInto(element, vnode.attrs);
    // Clicking an inline image opens it full-screen, and clicking a reply button sends its reply. The listeners are
    // delegated on the container, which mithril reuses across redraws, so they survive the innerHTML resets in
    // onupdate without needing to be re-attached.
    element.addEventListener("click", handleMarkdownImageClick);
    element.addEventListener("click", handleReplyButtonClick);
  },
  // Skip the subtree diff (and the onupdate innerHTML rewrite below) whenever the
  // markdown source is unchanged. onupdate re-sets innerHTML, which destroys every
  // text node in the subtree; the browser then collapses any selection anchored in
  // it. Since a global redraw fires on every scroll tick and every streamed event,
  // an unguarded rewrite kills a user's text selection on the first frame. The
  // rendered element is a childless div whose content we manage by hand, so there is
  // nothing for Mithril to diff -- retaining the DOM untouched is always correct when
  // the content matches. A change to whether the reply buttons can be pressed only
  // updates the buttons themselves (see onupdate).
  onbeforeupdate(vnode, old) {
    return (
      vnode.attrs.content !== old.attrs.content ||
      replyStateKey(vnode.attrs.replies) !== replyStateByElement.get(old.dom as Element)
    );
  },
  onupdate(vnode) {
    const element = vnode.dom as HTMLElement;
    if (renderedContentByElement.get(element) === vnode.attrs.content) {
      syncReplyButtons(element, vnode.attrs.replies);
      return;
    }
    const expanded = saveExpandedState(element);
    renderInto(element, vnode.attrs);
    restoreExpandedState(element, expanded);
  },
  view() {
    return m("div", { class: "message-content markdown-content" });
  },
};
