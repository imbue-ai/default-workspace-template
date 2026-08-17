// The embedder side of the minds embed contract, reusing the shared
// embed_contract.js module verbatim (one source of truth with the desktop
// shell and the workspace UI; see apps/minds/docs/embed-contract.md). This
// shim is the chrome's ONLY postMessage surface.

// eslint-disable-next-line
// @ts-ignore -- plain JS module shared with the desktop shell (no decls).
import * as contract from "../../../minds/imbue/minds/desktop_client/static/embed_contract.js";

export interface EmbedderEndpoint {
  send(type: string, payload?: Record<string, unknown>): void;
  dispose(): void;
}

export interface EmbedHandlers {
  onOpenAiKeysPage?: (hostId: string | undefined) => void;
}

export const OPEN_AI_KEYS_ACK: string = contract.OPEN_AI_KEYS_ACK;

export function createEmbedder(
  getFrameWindow: () => Window | null,
  isExpectedOrigin: (origin: string) => boolean,
  handlers: EmbedHandlers,
): EmbedderEndpoint {
  return contract.createEmbedderEndpoint({
    getFrameWindow,
    isExpectedOrigin,
    handlers: {
      [contract.OPEN_AI_KEYS_PAGE]: (message: { hostId?: string }) => {
        handlers.onOpenAiKeysPage?.(message.hostId);
      },
      [contract.OPEN_REQUEST_MODAL]: (message: { requestId?: string }) => {
        console.log("minds web: open-request-modal not yet supported", message);
      },
      [contract.OPEN_HELP]: (message: { agentId?: string }) => {
        console.log("minds web: open-help not yet supported", message);
      },
      [contract.BRING_APP_TO_FRONT]: () => {
        console.log("minds web: bring-app-to-front is a no-op in the browser");
      },
      [contract.CLOSE_ACTIVE_TAB]: () => {
        console.log("minds web: close-active-tab not yet supported");
      },
    },
  }) as EmbedderEndpoint;
}
