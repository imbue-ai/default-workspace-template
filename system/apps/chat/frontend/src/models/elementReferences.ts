/**
 * An element reference entering a composer becomes an attachment (element-reference-menu plan section 7): the
 * reference travels as a fenced ``json`` block in a draft's text until it reaches a chat, which takes each block
 * out of the text, uploads it as a ``REF-<id>.json`` file through the ordinary attachment path, and leaves the
 * prompt that names it ("Explain what I attached in REF-<id>") in the composer. The agent then opens the file as
 * it opens any attachment.
 */

import {
  referenceEnvelopeOf,
  referenceFileNameOf,
  referenceFileText,
  referenceSummaryOf,
  type ElementReferenceEnvelope,
} from "@imbue/workspace-ui/src/element_reference";
import { uploadDescribedFileToComposer } from "./ComposerAttachments";

/** A fenced ``json`` block with its object on one line, as ``jsonBlock`` writes it. */
const JSON_BLOCK_PATTERN = /```json\n([^\n]*)\n```/g;

/** The file a reference is attached as: its envelope, pretty-printed, named by its id. */
export function referenceFileOf(envelope: ElementReferenceEnvelope): File {
  const fileName = referenceFileNameOf(envelope.element_reference.reference_id);
  return new File([referenceFileText(envelope)], fileName, { type: "application/json" });
}

interface ReferenceBlock {
  start: number;
  end: number;
  envelope: ElementReferenceEnvelope;
}

function referenceBlocksIn(text: string): ReferenceBlock[] {
  const found: ReferenceBlock[] = [];
  for (const match of text.matchAll(JSON_BLOCK_PATTERN)) {
    const envelope = referenceEnvelopeOf(match[1]);
    if (envelope === null) continue;
    found.push({ start: match.index, end: match.index + match[0].length, envelope });
  }
  return found;
}

/** ``text`` without the blocks, the blank lines that set them off collapsed, and no trailing newlines (a prompt
 *  that ends in a space to type after keeps it). */
function withoutBlocks(text: string, blocks: readonly ReferenceBlock[]): string {
  const pieces: string[] = [];
  let cursor = 0;
  for (const block of blocks) {
    pieces.push(text.slice(cursor, block.start));
    cursor = block.end;
  }
  pieces.push(text.slice(cursor));
  return pieces
    .join("")
    .replace(/\n{3,}/g, "\n\n")
    .replace(/\n+$/, "");
}

/**
 * Stage every reference block in ``text`` as an attachment of ``chatId``'s composer and answer the text without
 * them. A text with no reference block is answered as it is, and nothing is uploaded.
 */
export function stageElementReferences(chatId: string, text: string): string {
  const blocks = referenceBlocksIn(text);
  if (blocks.length === 0) return text;
  for (const block of blocks) {
    uploadDescribedFileToComposer(
      chatId,
      referenceFileOf(block.envelope),
      referenceSummaryOf(block.envelope.element_reference),
    );
  }
  return withoutBlocks(text, blocks);
}
