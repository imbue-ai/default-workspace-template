/**
 * The harness logo, on its own path -- decoupled from the model bar.
 *
 * The logo is a pure function of the agent's harness, so it must never blink with the live
 * model choice, never wait on the `/api/harnesses` catalog fetch, and never vanish when the
 * model bar returns null. It is fetched once per agent from `/api/agents/:id/harness-logo`
 * and cached; nothing renders until that endpoint answers -- which is exactly when the agent
 * is real (a proto-agent 404s, so no logo shows for it). Once we have the SVG it never
 * changes for that agent, so the component skips re-diffing its trusted HTML entirely.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

const svgByAgent = new Map<string, string>();
const inFlight = new Set<string>();

async function loadLogo(agentId: string): Promise<void> {
  if (svgByAgent.has(agentId) || inFlight.has(agentId)) {
    return;
  }
  inFlight.add(agentId);
  try {
    const response = await m.request<{ svg: string }>({
      method: "GET",
      url: apiUrl("/api/agents/:agentId/harness-logo"),
      params: { agentId },
    });
    svgByAgent.set(agentId, response.svg);
    m.redraw();
  } catch {
    // Not resolvable yet (proto agent, or a transient) -- leave it uncached so a later
    // render retries. No logo shows meanwhile, which is the intended "not for proto" behavior.
  } finally {
    inFlight.delete(agentId);
  }
}

export const HarnessLogo: m.Component<{ agentId: string }> = {
  onbeforeupdate(vnode, old) {
    // Re-render only if the agent changed or we don't yet have its logo; once the SVG is
    // cached it is constant, so skip diffing (the trusted HTML is never re-injected).
    const agentId = vnode.attrs.agentId;
    return agentId !== (old.attrs as { agentId: string }).agentId || !svgByAgent.has(agentId);
  },
  view(vnode) {
    const { agentId } = vnode.attrs;
    const svg = svgByAgent.get(agentId);
    if (svg === undefined) {
      void loadLogo(agentId);
      return null;
    }
    return m("span", { class: "model-bar-logo", "aria-hidden": "true" }, m.trust(svg));
  },
};
