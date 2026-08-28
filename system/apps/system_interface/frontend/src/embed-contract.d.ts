/**
 * Type surface for the vendored minds embed contract module (see the vite
 * alias in vite.config.ts). The implementation is plain JS shipped by minds
 * (`vendor/mngr/apps/minds/imbue/minds/desktop_client/static/embed_contract.js`);
 * this declaration mirrors its exports -- update both together.
 */
declare module "@minds/embed-contract" {
  export const CONTRACT_VERSION: string;

  export const OPEN_REQUEST_MODAL: "minds:open-request-modal";
  export const OPEN_HELP: "minds:open-help";
  export const OPEN_AI_KEYS_PAGE: "minds:open-ai-keys-page";
  export const BRING_APP_TO_FRONT: "minds:bring-app-to-front";
  export const CLOSE_ACTIVE_TAB: "minds:close-active-tab";
  export const OPEN_AI_KEYS_ACK: "minds:open-ai-keys-ack";
  // The verdicts message (contract v3, which also retired v2's
  // permission-request-resolved). The vendored snapshot may predate it --
  // consumers probe the namespace before reading it (see embed.ts) rather
  // than named-importing, which would fail the build.
  export const PERMISSION_RESOLUTIONS: "minds:permission-resolutions";
  export const MAX_PERMISSION_RESOLUTION_ENTRIES: number;
  export const OPEN_SHARE_SETTINGS: "minds:open-share-settings";

  export const REQUEST_ID_PATTERN: RegExp;
  export const AGENT_ID_PATTERN: RegExp;
  export const HOST_ID_PATTERN: RegExp;
  export const SERVICE_NAME_PATTERN: RegExp;

  export type ContractMessage = { type: string } & Record<string, unknown>;

  export interface ContractEndpoint {
    send(type: string, payload?: Record<string, unknown>): void;
    dispose(): void;
  }

  export function createWorkspaceEndpoint(options: {
    handlers?: Record<string, (message: ContractMessage) => void>;
  }): ContractEndpoint;

  export function createEmbedderEndpoint(options: {
    getFrameWindow: () => Window | null;
    isExpectedOrigin?: (origin: string) => boolean;
    handlers?: Record<string, (message: ContractMessage) => void>;
  }): ContractEndpoint;
}
