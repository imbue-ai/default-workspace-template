/** The attrs a ``ChatRail`` takes, for tests: an empty rail in the wide layout, with every field overridable. */

import { scopeOfHandshake } from "@imbue/workspace-ui/src/element_reference";
import type { ChatRailAttrs } from "./ChatRail";

export function chatRailAttrsFixture(overrides: Partial<ChatRailAttrs> = {}): ChatRailAttrs {
  return {
    rows: [],
    selectedChatId: null,
    isInDrawer: false,
    isTouch: false,
    onPick: () => undefined,
    onNew: () => undefined,
    referenceScope: scopeOfHandshake(null),
    onDraftReference: () => undefined,
    isReferenceDraftAvailable: false,
    ...overrides,
  };
}
