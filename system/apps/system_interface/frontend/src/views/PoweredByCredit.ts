/**
 * The per-agent harness credit -- a plain, non-clickable label.
 *
 * The harness declares the WHOLE credit string ("Powered by Codex", "Powered by Pi Coding"),
 * prefix included; this renders it verbatim and adds nothing. A harness that wants no credit
 * declares "" (claude does), and then nothing renders at all.
 *
 * The credit is a pure function of the agent's harness, so it must never blink with the live
 * model choice, never wait on the `/api/harnesses` catalog fetch, and never vanish when the
 * model bar returns null. It is fetched once per agent from `/api/agents/:id/powered-by` and
 * cached; nothing renders until that endpoint answers -- which is exactly when the agent is
 * real (a proto-agent 404s, so no credit shows for it). Once we have the text it never
 * changes for that agent.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

const labelByAgent = new Map<string, string>();
const inFlight = new Set<string>();

async function loadLabel(agentId: string): Promise<void> {
  if (labelByAgent.has(agentId) || inFlight.has(agentId)) {
    return;
  }
  inFlight.add(agentId);
  try {
    const response = await m.request<{ label: string }>({
      method: "GET",
      url: apiUrl("/api/agents/:agentId/powered-by"),
      params: { agentId },
    });
    labelByAgent.set(agentId, response.label);
    m.redraw();
  } catch {
    // Not resolvable yet (proto agent, or a transient) -- leave it uncached so a later
    // render retries. No credit shows meanwhile, which is the intended "not for proto" behavior.
  } finally {
    inFlight.delete(agentId);
  }
}

export const PoweredByCredit: m.Component<{ agentId: string }> = {
  view(vnode) {
    const { agentId } = vnode.attrs;
    const label = labelByAgent.get(agentId);
    if (label === undefined) {
      void loadLabel(agentId);
      return null;
    }
    // "" is a real, cached answer -- the harness declaring "show no credit" -- not a missing
    // one, so this renders nothing WITHOUT re-fetching (the `undefined` check above already
    // handled "not loaded yet").
    if (label === "") {
      return null;
    }
    // A static span (not a button): same font as the neighbouring action buttons, but not
    // interactive and not focusable. The text is the harness's verbatim string. Centered
    // over the under-bar as an out-of-flow overlay: pointer-events-none so it never
    // intercepts clicks on the model bar or buttons beneath it; max-w keeps it clear of
    // the side groups, ellipsizing if it would collide.
    return m(
      "span",
      {
        class:
          "composer-under-bar-credit pointer-events-none absolute top-1/2 left-1/2 inline-flex h-[30px] " +
          "max-w-[60%] -translate-x-1/2 -translate-y-1/2 items-center overflow-hidden px-2 font-sans " +
          "text-(length:--font-size-body) text-ellipsis whitespace-nowrap text-secondary select-none",
      },
      label,
    );
  },
};
