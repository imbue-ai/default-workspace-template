/**
 * The rows of a page's element menu (docs/system/blueprint/element-reference-menu/, section
 * 3.3): the standard rows the browser's own menu would have offered the target (Cut, Copy,
 * Paste, Select All, the link rows, the image row) and the reference rows that hand the
 * element to a chat (Copy reference, Explain, Modify).
 *
 * A row here is structurally an ``ActionRow`` or a ``DividerRow`` of the shared Menu component,
 * so a Mithril page passes the rows straight to ``createMenu`` and the framework-free renderer
 * of ``context_menu.ts`` draws the same objects. This module imports no framework.
 */

import {
  contentEditableRegionOf,
  describeElement,
  elementOfTarget,
  isEditableElement,
  imageSrcOf,
  linkHrefOf,
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
  onSelect: () => void;
}

export interface ContextMenuDividerRow {
  kind: "divider";
}

export type ContextMenuRow = ContextMenuActionRow | ContextMenuDividerRow;

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

/** The input types the selection API (``selectionStart``, ``setRangeText``) applies to; on any other type a
 *  browser throws, so an editable input of another type (``email``, ``number``, ``date``) takes the
 *  ``execCommand`` path a contenteditable region takes. */
const SELECTION_API_INPUT_TYPES: ReadonlySet<string> = new Set(["text", "search", "url", "tel", "password"]);

/** Whether the target is a text control the field API acts on: a textarea, or an input of a type it applies to. */
function isFieldElement(element: Element): element is HTMLInputElement | HTMLTextAreaElement {
  if (element instanceof HTMLTextAreaElement) return true;
  return element instanceof HTMLInputElement && SELECTION_API_INPUT_TYPES.has(element.type);
}

/** The text a field has selected, or "" (a contenteditable region's selection is the document's). */
function fieldSelectionOf(element: Element): string {
  if (!isFieldElement(element)) return "";
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
  (element as HTMLElement).focus();
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
  (element as HTMLElement).focus();
  element.ownerDocument.execCommand("insertText", false, text);
}

function selectAllIn(element: Element): void {
  if (isFieldElement(element)) {
    element.focus();
    element.select();
    return;
  }
  (element as HTMLElement).focus();
  element.ownerDocument.execCommand("selectAll");
}

/** Whether this browser lets a page read the clipboard: Paste is offered only where it can run. */
export function canReadClipboard(): boolean {
  return typeof navigator !== "undefined" && typeof navigator.clipboard?.readText === "function";
}

/** The standard rows the target admits (section 3.3), in the order the native menu lists them. */
export function standardContextMenuRows(target: ContextMenuTarget): ContextMenuRow[] {
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
  const href = linkHrefOf(element);
  if (href !== null) {
    mediaRows.push(
      { kind: "action", key: "copy-link", label: "Copy link address", onSelect: () => void copyText(href) },
      {
        kind: "action",
        key: "open-link",
        label: "Open link in new window",
        onSelect: () => void element.ownerDocument.defaultView?.open(href, "_blank", "noopener"),
      },
    );
  }
  const src = imageSrcOf(element);
  if (src !== null) {
    mediaRows.push({
      kind: "action",
      key: "copy-image",
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
    { kind: "action", key: "copy-element-path", label: "Copy reference", onSelect: () => void copyText(block) },
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

/** The whole element menu (section 3.3): the page's own rows, the standard rows, the reference rows. */
export function elementMenuRows(
  target: ContextMenuTarget,
  scope: ReferenceScope,
  draft: (text: string) => void,
  isDraftAvailable: boolean,
  ownRows: readonly ContextMenuRow[],
): ContextMenuRow[] {
  const reference = describeElement(target.element, target.click, scope);
  return joinRowGroups([
    ownRows,
    standardContextMenuRows(target),
    elementReferenceRows(reference, draft, isDraftAvailable),
  ]);
}
