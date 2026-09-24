/**
 * The spill (element-reference-menu plan section 7.2): a reference block too large for a
 * composer goes to a reference file through the chat app, and the composer takes the pointer
 * form instead. Run at the two places a draft enters a composer -- the root applying an intake
 * or drafting from its rail, and a chat page drafting from its own menu -- before
 * ``prependToComposer``.
 */

import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import {
  isOversizeBlock,
  jsonBlock,
  pointerFormOf,
  referenceEnvelopeOf,
  type ElementReferenceEnvelope,
} from "@imbue/workspace-ui/src/element_reference";
import { postJson } from "@imbue/workspace-ui/src/models/http";

/** A fenced ``json`` block with its object on one line, as ``jsonBlock`` writes it. */
const JSON_BLOCK_PATTERN = /```json\n([^\n]*)\n```/g;

/** Write an envelope to a reference file through the chat app; answers the file's path. */
export async function storeElementReference(envelope: ElementReferenceEnvelope): Promise<string> {
  const answer = await postJson<{ path: string }>(apiUrl("/api/element-references"), { reference: envelope });
  return answer.path;
}

interface OversizeBlock {
  start: number;
  end: number;
  envelope: ElementReferenceEnvelope;
}

function oversizeBlocksIn(text: string): OversizeBlock[] {
  const found: OversizeBlock[] = [];
  for (const match of text.matchAll(JSON_BLOCK_PATTERN)) {
    if (!isOversizeBlock(match[0])) continue;
    const envelope = referenceEnvelopeOf(match[1]);
    if (envelope === null) continue;
    found.push({ start: match.index, end: match.index + match[0].length, envelope });
  }
  return found;
}

/**
 * ``text`` with every oversize reference block replaced by the pointer form of the file it was
 * written to. A text with none is answered as it is without a request; a block whose write
 * failed stays as it was, with a warning, so a draft is never lost to the spill.
 */
export async function spillOversizeElementReferences(text: string): Promise<string> {
  const blocks = oversizeBlocksIn(text);
  if (blocks.length === 0) return text;
  const pieces: string[] = [];
  let cursor = 0;
  for (const block of blocks) {
    pieces.push(text.slice(cursor, block.start));
    try {
      const path = await storeElementReference(block.envelope);
      pieces.push(jsonBlock(pointerFormOf(block.envelope.element_reference, path)));
    } catch (error) {
      console.warn(`[chat] could not write the element reference to a file: ${(error as Error).message}`);
      pieces.push(text.slice(block.start, block.end));
    }
    cursor = block.end;
  }
  pieces.push(text.slice(cursor));
  return pieces.join("");
}
