/**
 * The location-beacon boundary: the one place the shell listens for messages
 * from the workspace's own framed apps.
 *
 * This is the second sanctioned cross-frame message surface, beside the embed
 * contract (`src/embed.ts`, which owns the chrome<->workspace channel): a
 * framed app posts `{type: "shell:location", path}` (the workspace app model's
 * contract message; the older `minds-location` spelling is still accepted) to
 * `window.parent` -- one hop up, which is this shell -- on each page load, so the instance it shows
 * can reopen at the same place (see the vendored file viewer's beacon and the
 * `build-app` scaffold). It lives in its own module for the same reason the
 * embed contract does: every raw `message` listener is confined by the embed
 * ratchet (`test_embed_ratchets.py`) to an allowlisted file, so the whole
 * surface stays greppable and auditable, source checks included.
 *
 * The shell trusts nothing about a beacon: the origin must be one of this
 * workspace's own service origins (derived from the registry exactly as the
 * panes' URLs are), the posting window must belong to a pane this dock is
 * rendering, and that pane must show an app instance -- the only objects with
 * a location to keep. What survives is stored by the instance's ref,
 * machine-wide (see models/MemberLocations).
 */

import { getApps, labelForService } from "./models/AgentManager";
import { recordMemberLocation } from "./models/MemberLocations";
import { instanceNameFromRef } from "./models/Projects";
import { deriveServiceOrigin } from "./origin";
import { IFRAME_PANEL_LIVE_KEY_ATTR } from "./views/IframePanel";

function handleLocationBeaconMessage(event: MessageEvent): void {
  const data = event.data as { type?: unknown; path?: unknown } | null;
  // CLEANUP: drop the "minds-location" spelling in phase 7 of the workspace app model, when
  // this listener is replaced by the contract listener in DockviewWorkspace.ts.
  if (data === null || typeof data !== "object") return;
  if (data.type !== "shell:location" && data.type !== "minds-location") return;
  if (typeof data.path !== "string" || data.path === "") return;
  // The sender must be one of this workspace's own services: their origins
  // are derived from the registry exactly as the panes' URLs are, so the
  // comparison is against what this shell itself would frame.
  const serviceOrigins = new Set(
    getApps().map((app) => deriveServiceOrigin(labelForService(app.name)).replace(/\/$/, "")),
  );
  if (!serviceOrigins.has(event.origin)) return;
  // Resolve WHICH pane posted by its window, then which object that pane
  // shows by its live key -- the instance's ref.
  const iframes = document.querySelectorAll<HTMLIFrameElement>(`iframe[${IFRAME_PANEL_LIVE_KEY_ATTR}]`);
  for (const iframe of iframes) {
    if (iframe.contentWindow !== event.source) continue;
    const liveKey = iframe.getAttribute(IFRAME_PANEL_LIVE_KEY_ATTR);
    if (liveKey === null || instanceNameFromRef(liveKey) === null) return;
    recordMemberLocation(liveKey, data.path);
    return;
  }
}

/** Start listening for location beacons. Called once, when the dock is
 *  initialized -- the listener outlives every view the dock mounts, exactly
 *  as the live pages it locates do. */
export function initializeLocationBeaconListener(): void {
  window.addEventListener("message", handleLocationBeaconMessage);
}
