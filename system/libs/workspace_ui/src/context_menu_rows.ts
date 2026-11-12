/**
 * The rows of a page's element menu (docs/system/blueprint/element-reference-menu/, section
 * 3.3): the standard rows the browser's own menu would have offered the target (Cut, Copy,
 * Paste, Select All, the link rows, the image rows) and the reference rows that hand the
 * element to a chat (Copy reference, Explain, Modify). Also which right-clicks the menu leaves
 * to the Imbue Studio desktop app's native menu (section 11).
 *
 * A row here is structurally a row of the shared Menu component (an action, a check row, a
 * divider, or a submenu of those), so a Mithril page passes the rows straight to ``createMenu``
 * and the framework-free renderer of ``context_menu.ts`` draws the same objects. This module
 * imports no framework: its one import from the Menu is of types alone.
 */

import type { MenuRowIcon, MenuRowTone } from "./components/menu";
import {
  contentEditableRegionOf,
  describeElement,
  elementOfTarget,
  isEditableElement,
  imageSrcOf,
  isPasswordField,
  referenceBlock,
  selectionOf,
  type ElementReference,
  type ReferenceClick,
  type ReferenceScope,
} from "./element_reference";

/** A row that does one thing when picked. The optional fields mirror the shared Menu's ``ActionRow``. */
export interface ContextMenuActionRow {
  kind: "action";
  key: string;
  label: string;
  isDisabled?: boolean;
  tooltip?: string;
  tone?: MenuRowTone;
  icon?: MenuRowIcon;
  onSelect: () => void;
}

/** A row with a check mark that a pick toggles. Mirrors the shared Menu's ``CheckRow``. */
export interface ContextMenuCheckRow {
  kind: "check";
  key: string;
  label: string;
  isChecked: boolean;
  tooltip?: string;
  onToggle: () => void;
}

export interface ContextMenuDividerRow {
  kind: "divider";
}

/** What a submenu may hold: every row but another submenu. */
export type ContextMenuSubmenuChildRow = ContextMenuActionRow | ContextMenuCheckRow | ContextMenuDividerRow;

/** A row that opens a submenu of rows, one level deep. Mirrors the shared Menu's ``SubmenuRow``. ``rows`` is read
 *  each time the submenu opens or redraws, so it should only read the current state. */
export interface ContextMenuSubmenuRow {
  kind: "submenu";
  key: string;
  label: string;
  tooltip?: string;
  rows: () => readonly ContextMenuSubmenuChildRow[];
}

/** A row of the element menu, a page's own included: the one vocabulary the shared Menu and the framework-free
 *  renderer both draw, so a row kind added here reaches every app's menu. */
export type ContextMenuRow = ContextMenuSubmenuChildRow | ContextMenuSubmenuRow;

/** What a right-click landed on, captured when it happened. */
export interface ContextMenuTarget {
  element: Element;
  selectionText: string;
  click: ReferenceClick;
}

/** The prompt each reference row drafts beside the block, calling the reference by its id: the chat attaches the
 *  block as a file of that name, so the message names what it attached. */
export function explainPromptOf(referenceId: string): string {
  return `Explain what I attached in ${referenceId}`;
}
export function modifyPromptOf(referenceId: string): string {
  return `Change ${referenceId} to `;
}
/** Why Explain and Modify are greyed on a page no shell frames. */
export const NO_SHELL_DRAFT_REASON = "Open this page in the workspace to draft into a chat";

/** The element a ``contextmenu`` event fired on: its target when that is an element, else the element it was
 *  bound to. */
export function targetElementOf(event: MouseEvent): Element {
  if (event.target instanceof Element) return event.target;
  return event.currentTarget as Element;
}

/** The target of a ``contextmenu`` event: its element, the selection at that moment, and the click. */
export function targetOfEvent(event: MouseEvent, ownerDocument: Document): ContextMenuTarget {
  return {
    element: elementOfTarget(event.target, ownerDocument),
    selectionText: selectionOf(ownerDocument).text,
    click: { clientX: event.clientX, clientY: event.clientY, pageX: event.pageX, pageY: event.pageY },
  };
}

/** The draft text a reference row hands over: the prompt, a blank line, the block. A chat takes the block out
 *  and attaches it as a file, leaving the prompt in the composer. */
export function draftTextOf(prompt: string, block: string): string {
  return `${prompt}\n\n${block}`;
}

function reportFailure(what: string, error: unknown): void {
  console.warn(`[context-menu] ${what} failed: ${(error as Error)?.message ?? String(error)}`);
}

/** Put ``text`` on the clipboard; answers whether it landed, reporting a refusal to the console. */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (error) {
    reportFailure("copy", error);
    return false;
  }
}

/** Whether the page can hand the image's pixels back: one from the page's own origin or inline in it, or one
 *  loaded with CORS. A canvas refuses to give back any other image drawn on it. */
function isImageReadable(image: HTMLImageElement): boolean {
  if (!image.complete || image.naturalWidth === 0) return false;
  if (image.crossOrigin !== null) return true;
  let url: URL;
  try {
    url = new URL(image.currentSrc || image.src, image.ownerDocument.baseURI);
  } catch {
    return false;
  }
  if (url.protocol === "data:") return true;
  return url.origin === image.ownerDocument.defaultView?.location.origin;
}

/** Whether "Copy image" can run on the image: the browser writes images to the clipboard, and the page can read
 *  this one. */
export function canCopyImage(image: HTMLImageElement): boolean {
  if (typeof ClipboardItem === "undefined" || typeof navigator.clipboard?.write !== "function") return false;
  return isImageReadable(image);
}

/** Put the image itself on the clipboard, as the PNG a canvas makes of it (the one image type every browser
 *  writes); answers whether it landed, reporting a refusal to the console. */
export async function copyImage(image: HTMLImageElement): Promise<boolean> {
  const canvas = image.ownerDocument.createElement("canvas");
  canvas.width = image.naturalWidth;
  canvas.height = image.naturalHeight;
  try {
    const context = canvas.getContext("2d");
    if (context === null) throw new Error("the canvas has no 2d context");
    context.drawImage(image, 0, 0);
    // Handed over as a promise, so the write starts inside the pick's user activation.
    const png = new Promise<Blob>((resolve, reject) => {
      canvas.toBlob(
        (blob) => (blob === null ? reject(new Error("the canvas made no image")) : resolve(blob)),
        "image/png",
      );
    });
    await navigator.clipboard.write([new ClipboardItem({ "image/png": png })]);
    return true;
  } catch (error) {
    reportFailure("copy image", error);
    return false;
  }
}

/** The input types the selection API (``selectionStart``, ``setRangeText``) applies to; on any other type a
 *  browser throws, so an editable input of another type (``email``, ``number``, ``date``) takes the
 *  ``execCommand`` path a contenteditable region takes. */
const SELECTION_API_INPUT_TYPES: ReadonlySet<string> = new Set(["text", "search", "url", "tel", "password"]);

/** Whether the target is a text control the field API acts on: a textarea, or an input of a type it applies to. */
function isFieldElement(element: Element): element is HTMLInputElement | HTMLTextAreaElement {
  if (element instanceof HTMLTextAreaElement) return true;
  return element instanceof HTMLInputElement && SELECTION_API_INPUT_TYPES.has(element.type);
}

/** The text a field has selected, or "" (a contenteditable region's selection is the document's). A password
 *  field's selection is never read, so Cut and Copy are not offered there, as the browser's own menu withholds
 *  them: the secret must not land on the clipboard in plain text. */
function fieldSelectionOf(element: Element): string {
  if (!isFieldElement(element) || isPasswordField(element)) return "";
  const start = element.selectionStart ?? 0;
  const end = element.selectionEnd ?? 0;
  return element.value.slice(start, end);
}

/** The text selected inside a contenteditable target's own region, or "": a selection elsewhere on the page is
 *  not the target's, so Cut does not offer to delete it. */
function regionSelectionOf(element: Element, selectionText: string): string {
  const region = contentEditableRegionOf(element);
  if (region === null || selectionText === "") return "";
  const selection = element.ownerDocument.getSelection();
  if (selection === null || selection.rangeCount === 0) return "";
  return region.contains(selection.getRangeAt(0).commonAncestorContainer) ? selectionText : "";
}

/** The element that takes focus for the ``execCommand`` path: the contenteditable region the target sits in
 *  (a child of the region is not itself focusable, and the command acts on the focused editor), else the
 *  target. */
function editableHostOf(element: Element): HTMLElement {
  return (contentEditableRegionOf(element) ?? element) as HTMLElement;
}

/** Cut fails whole, as the native row does: nothing is deleted unless the copy landed. */
async function cutFrom(element: Element, selectionText: string): Promise<void> {
  if (isFieldElement(element)) {
    const selected = fieldSelectionOf(element);
    if (selected === "") return;
    if (!(await copyText(selected))) return;
    element.setRangeText("", element.selectionStart ?? 0, element.selectionEnd ?? 0, "end");
    element.dispatchEvent(new Event("input", { bubbles: true }));
    return;
  }
  if (!(await copyText(selectionText))) return;
  editableHostOf(element).focus();
  element.ownerDocument.execCommand("delete");
}

async function pasteInto(element: Element): Promise<void> {
  let text: string;
  try {
    text = await navigator.clipboard.readText();
  } catch (error) {
    reportFailure("paste", error);
    return;
  }
  if (isFieldElement(element)) {
    element.focus();
    element.setRangeText(text, element.selectionStart ?? 0, element.selectionEnd ?? 0, "end");
    element.dispatchEvent(new Event("input", { bubbles: true }));
    return;
  }
  editableHostOf(element).focus();
  element.ownerDocument.execCommand("insertText", false, text);
}

function selectAllIn(element: Element): void {
  if (isFieldElement(element)) {
    element.focus();
    element.select();
    return;
  }
  editableHostOf(element).focus();
  element.ownerDocument.execCommand("selectAll");
}

/** Whether this browser lets a page read the clipboard: Paste is offered only where it can run. */
export function canReadClipboard(): boolean {
  return typeof navigator !== "undefined" && typeof navigator.clipboard?.readText === "function";
}

/** Open a link the menu was opened on, as a click on it would. */
export type OpenLink = (anchor: HTMLAnchorElement) => void;

/** The standard rows the target admits (section 3.3), in the order the native menu lists them. */
export function standardContextMenuRows(target: ContextMenuTarget, openLink: OpenLink): ContextMenuRow[] {
  const { element, selectionText } = target;
  const isEditable = isEditableElement(element);
  const fieldSelection = fieldSelectionOf(element);
  const hasSelection = selectionText !== "" || fieldSelection !== "";
  // Cut acts on the target's own selection: a field's, or the document's when it lies inside the target's
  // contenteditable region.
  const ownSelection = isFieldElement(element) ? fieldSelection : regionSelectionOf(element, selectionText);
  const editRows: ContextMenuRow[] = [];
  if (isEditable && ownSelection !== "") {
    editRows.push({ kind: "action", key: "cut", label: "Cut", onSelect: () => void cutFrom(element, selectionText) });
  }
  if (hasSelection) {
    editRows.push({
      kind: "action",
      key: "copy",
      label: "Copy",
      onSelect: () => void copyText(fieldSelection !== "" ? fieldSelection : selectionText),
    });
  }
  if (isEditable && canReadClipboard()) {
    editRows.push({ kind: "action", key: "paste", label: "Paste", onSelect: () => void pasteInto(element) });
  }
  if (isEditable) {
    editRows.push({ kind: "action", key: "select-all", label: "Select All", onSelect: () => selectAllIn(element) });
  }
  const mediaRows: ContextMenuRow[] = [];
  const anchor = element.closest("a[href]");
  if (anchor instanceof HTMLAnchorElement) {
    const href = anchor.href;
    mediaRows.push(
      { kind: "action", key: "copy-link", label: "Copy link address", onSelect: () => void copyText(href) },
      { kind: "action", key: "open-link", label: "Open link", onSelect: () => openLink(anchor) },
    );
  }
  const src = imageSrcOf(element);
  if (src !== null) {
    const image = element as HTMLImageElement;
    if (canCopyImage(image)) {
      mediaRows.push({
        kind: "action",
        key: "copy-image",
        label: "Copy image",
        onSelect: () => void copyImage(image),
      });
    }
    mediaRows.push({
      kind: "action",
      key: "copy-image-address",
      label: "Copy image address",
      onSelect: () => void copyText(src),
    });
  }
  return joinRowGroups([editRows, mediaRows]);
}

/** The reference rows: the block to the clipboard, or a prompt and the block to a chat's composer. */
export function elementReferenceRows(
  reference: ElementReference,
  draft: (text: string) => void,
  isDraftAvailable: boolean,
): ContextMenuRow[] {
  const block = referenceBlock(reference);
  const draftRow = (key: string, label: string, prompt: string): ContextMenuActionRow => ({
    kind: "action",
    key,
    label,
    isDisabled: !isDraftAvailable,
    tooltip: isDraftAvailable ? undefined : NO_SHELL_DRAFT_REASON,
    onSelect: () => draft(draftTextOf(prompt, block)),
  });
  return [
    { kind: "action", key: "copy-reference", label: "Copy reference", onSelect: () => void copyText(block) },
    draftRow("explain-element", "Explain...", explainPromptOf(reference.reference_id)),
    draftRow("modify-element", "Modify...", modifyPromptOf(reference.reference_id)),
  ];
}

/** The groups in order with one divider between each pair of non-empty ones; empty groups vanish. */
export function joinRowGroups(groups: readonly (readonly ContextMenuRow[])[]): ContextMenuRow[] {
  const rows: ContextMenuRow[] = [];
  for (const group of groups) {
    if (group.length === 0) continue;
    if (rows.length > 0) rows.push({ kind: "divider" });
    rows.push(...group);
  }
  return rows;
}

/** Whether the click landed on the document's selection: inside one of the boxes its ranges are drawn as. */
function isClickOnSelection(target: ContextMenuTarget): boolean {
  const selection = target.element.ownerDocument.getSelection();
  if (selection === null || selection.isCollapsed) return false;
  const { clientX, clientY } = target.click;
  for (let index = 0; index < selection.rangeCount; index += 1) {
    const range = selection.getRangeAt(index);
    // A range's boxes are a rendering fact; an environment without layout draws the selection nowhere.
    if (typeof range.getClientRects !== "function") continue;
    for (const box of Array.from(range.getClientRects())) {
      if (clientX >= box.left && clientX <= box.right && clientY >= box.top && clientY <= box.bottom) return true;
    }
  }
  return false;
}

/**
 * Whether a right-click is one the Imbue Studio desktop app's native menu answers better than the page's
 * (section 11): text being edited, the selection under the pointer, or a link the page has no rows of its own
 * for. The native menu carries what a page cannot offer there (spelling suggestions, Look Up, a paste that asks
 * no permission) and none of the page's rows. An image keeps the page's menu, inside a link or not.
 */
export function isNativeMenuTarget(target: ContextMenuTarget, hasOwnRows: boolean): boolean {
  const { element } = target;
  if (isEditableElement(element) || isClickOnSelection(target)) return true;
  if (hasOwnRows || imageSrcOf(element) !== null) return false;
  return element.closest("a[href]") !== null;
}

/** The whole element menu (section 3.3): the page's own rows, the standard rows, the reference rows. */
export function elementMenuRows(
  target: ContextMenuTarget,
  scope: ReferenceScope,
  draft: (text: string) => void,
  isDraftAvailable: boolean,
  ownRows: readonly ContextMenuRow[],
  openLink: OpenLink,
): ContextMenuRow[] {
  const reference = describeElement(target.element, target.click, scope);
  return joinRowGroups([
    ownRows,
    standardContextMenuRows(target, openLink),
    elementReferenceRows(reference, draft, isDraftAvailable),
  ]);
}
