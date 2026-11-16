/** The service's ``GET /api/state`` document and its events (docs/system/specs/screen/spec.md section 10). */

export type Lifecycle = "installing" | "starting" | "running" | "stopped";
export type Controller = "human" | "agent" | "nobody";

export interface ControlDocument {
  controller: Controller;
  human_pinned: boolean;
  human_session_id: string | null;
  human_name: string | null;
  agent_id: string | null;
  agent_name: string | null;
  waiting: string[];
  idle_seconds: number | null;
  idle_release_seconds: number | null;
}

export interface ScreenSizeDocument {
  width: number;
  height: number;
  rate: number;
}

/** The browser on the Screen (spec section 16): the one browser's lifecycle, its name, and a page the human asked
 *  to open that is held while an agent drives. */
export interface ChromiumDocument {
  state: "absent" | "init" | "running" | "crashed" | "stopped";
  name: string | null;
  pending_url: string | null;
}

export interface ScreenStateDocument {
  lifecycle: Lifecycle;
  install_reason: string | null;
  stop_reason: string | null;
  title: string;
  video_id: string;
  control: ControlDocument;
  screen_size: ScreenSizeDocument | null;
  chromium: ChromiumDocument;
}

export type ServiceEvent =
  | ({ type: "state" } & ScreenStateDocument)
  | { type: "stopped"; reason: string; member?: string }
  | { type: "profile"; video_id: string }
  | { type: "clipboard"; kind: string }
  | { type: "handoff_request"; agent_name: string; reason: string };

export interface ScreenSizeAnswer {
  applied: boolean;
  reason: string | null;
  screen_size: ScreenSizeDocument | null;
}

export interface NekoSessionAnswer {
  session_id: string;
  token: string;
  name: string;
  is_owner: boolean;
}

/** Where the Screen's ICE mux port is reachable from this client (spec section 5.2); ``protocol`` is ``udp`` or
 *  ``tcp``, lower-case. */
export interface EndpointCandidate {
  host: string;
  port: number;
  protocol: string;
}

export interface ConnectInfo {
  ice_servers: RTCIceServer[];
  candidates: EndpointCandidate[];
  expires_at: string | null;
}

export const EMPTY_CONNECT_INFO: ConnectInfo = { ice_servers: [], candidates: [], expires_at: null };

/** What the strip says about who has control (spec section 4.2). */
export function describeController(control: ControlDocument, ownSessionId: string | null): string {
  switch (control.controller) {
    case "human":
      if (isUnclaimedHandoff(control)) return "Handed to you";
      return control.human_session_id !== null && control.human_session_id === ownSessionId
        ? "You"
        : (control.human_name ?? "Someone");
    case "agent":
      return `Agent ${control.agent_name ?? control.agent_id ?? ""}`.trim();
    case "nobody":
      return "Nobody";
  }
}

export function isOwnSessionController(control: ControlDocument, ownSessionId: string | null): boolean {
  return control.controller === "human" && ownSessionId !== null && control.human_session_id === ownSessionId;
}

/** An agent handed the Screen to the human and nobody has claimed it yet: the lease is the human's, pinned, with
 *  no session, so any human's first input (or Take control) claims it and no agent takes it meanwhile. */
export function isUnclaimedHandoff(control: ControlDocument): boolean {
  return control.controller === "human" && control.human_session_id === null;
}

/** Whether a viewer's input may flow without its session holding control: nobody controls (the first input is
 *  neko's control/request, granted with no host), or a handoff waits to be claimed the same way. */
export function isOpenToFirstInput(control: ControlDocument): boolean {
  return control.controller === "nobody" || isUnclaimedHandoff(control);
}
