/**
 * The write side of the model bar: an optimistic pick, reconciled from the pushed
 * live choice (not a poll).
 *
 * The live selection is read from each agent's `model_choice` on the agents store
 * (pushed over the WebSocket); this module only owns the optimistic overlay while a
 * pick is being applied. On a pick we show it immediately (`pending`), POST it, and
 * clear the overlay once the pushed live choice matches it (or a timeout elapses, so
 * a switch the harness silently refused cannot strand the chip on a lie).
 *
 * Picks for one agent run through a single-flight chain so rapid clicks reach the
 * backend in click order -- without it the threaded backend could deliver the
 * commands out of order and leave the agent in the wrong state.
 */

import m from "mithril";
import { apiUrl } from "../base-path";
import { getAgentById } from "./AgentManager";
import type { CatalogModelOption } from "./HarnessCatalog";

export interface ModelIdentity {
  model_id: string;
  effort: string | null;
  fast: boolean;
}

export interface ModelChoice {
  identity: ModelIdentity;
  // The catalog option the live identity matched (computed on the backend), or null (shrug).
  matched: CatalogModelOption | null;
}

interface PendingPick {
  identity: ModelIdentity;
  // The option the user clicked -- rendered directly, so no client-side matching.
  option: CatalogModelOption;
}

// The optimistic overlay per agent, and the tail of each agent's apply chain.
const pendingByAgent = new Map<string, PendingPick>();
const applyChainByAgent = new Map<string, Promise<void>>();

// How long an optimistic pick is held before it is forcefully reset to the live
// truth, if no matching live choice ever arrives (e.g. a switch the harness
// refused, or a stuck in-flight send). Generous, because the bar stays fully
// interactive meanwhile -- this is only the last-resort self-heal.
const PENDING_TIMEOUT_MS = 5 * 60 * 1000;

// Two picks are the same axis-for-axis. The model id is compared exactly because a
// pick always carries a catalog id (never a raw reported id); used only to expire the
// right pending overlay, not to reconcile against the live choice (see effectiveChoice).
function pickIdentityEquals(a: ModelIdentity, b: ModelIdentity): boolean {
  return a.model_id === b.model_id && (a.effort ?? null) === (b.effort ?? null) && a.fast === b.fast;
}

export interface EffectiveChoice {
  identity: ModelIdentity;
  matched: CatalogModelOption | null;
  isPending: boolean;
}

/**
 * What the bar should render for an agent: the optimistic pick while one is in
 * flight, otherwise the live choice pushed onto the agents store. The pending
 * overlay is held (as a whole) until the live choice matches it -- so the sequence
 * of per-command settings writes a harness makes never flickers the chip through an
 * intermediate state -- then cleared. Returns null when there is nothing to show yet.
 */
export function effectiveChoice(agentId: string, liveChoice: ModelChoice | null | undefined): EffectiveChoice | null {
  const pending = pendingByAgent.get(agentId);
  if (pending) {
    // The pushed identity carries a raw reported id, so we reconcile against the option the
    // backend matched it to (not the raw id): the overlay settles once the matched option is
    // the one the user clicked and effort/fast agree. A live read that matched nothing (shrug)
    // never settles a pick -- the overlay holds until it does, or the timeout fires.
    const settled =
      liveChoice != null &&
      liveChoice.matched != null &&
      liveChoice.matched.id === pending.option.id &&
      (liveChoice.identity.effort ?? null) === (pending.identity.effort ?? null) &&
      liveChoice.identity.fast === pending.identity.fast;
    if (settled) {
      pendingByAgent.delete(agentId);
    } else {
      return { identity: pending.identity, matched: pending.option, isPending: true };
    }
  }
  if (liveChoice == null) {
    return null;
  }
  return { identity: liveChoice.identity, matched: liveChoice.matched, isPending: false };
}

/** Which axes differ between the value the user was looking at (`prev`) and the
 *  one they just picked (`next`). Sent to the backend so it applies only those --
 *  computed here, against the optimistic overlay, so re-picking the value you
 *  started on (medium -> xhigh -> medium) still sends /effort medium rather than
 *  being suppressed by a disk read that has not caught up. Both model ids are catalog
 *  ids here (the caller passes the matched option's id as `prev`, never the raw
 *  reported id), so an exact compare is correct -- an effort/fast click keeps the same
 *  model id and does NOT re-send /model. */
export function changedAxes(prev: ModelIdentity, next: ModelIdentity): string[] {
  const axes: string[] = [];
  if (prev.model_id !== next.model_id) {
    axes.push("model");
  }
  if ((prev.effort ?? null) !== (next.effort ?? null)) {
    axes.push("effort");
  }
  if (prev.fast !== next.fast) {
    axes.push("fast");
  }
  return axes;
}

/** Apply the `axes` a click changed, then POST it. `axes` names which of
 *  model/effort/fast to actually send (see changedAxes).
 *
 *  `optimistic` follows the harness's switch mode: an EAGER_THEN_RECONCILE harness
 *  (all three -- claude, codex, pi) shows the pick immediately and reconciles from
 *  the pushed live choice. */
export function setModelChoice(
  agentId: string,
  identity: ModelIdentity,
  option: CatalogModelOption,
  axes: string[],
  optimistic = true,
): void {
  if (optimistic) {
    pendingByAgent.set(agentId, { identity, option });
    m.redraw();
  }

  const previous = applyChainByAgent.get(agentId) ?? Promise.resolve();
  const next = previous.then(
    () => postModelChoice(agentId, identity, axes),
    () => postModelChoice(agentId, identity, axes),
  );
  applyChainByAgent.set(agentId, next);
  void next.then(() => {
    if (applyChainByAgent.get(agentId) === next) {
      applyChainByAgent.delete(agentId);
    }
    if (optimistic) {
      schedulePendingTimeout(agentId, identity);
    }
  });
}

async function postModelChoice(agentId: string, identity: ModelIdentity, axes: string[]): Promise<void> {
  try {
    await m.request({
      method: "POST",
      url: apiUrl("/api/chats/:agentId/model"),
      params: { agentId },
      body: { model_id: identity.model_id, effort: identity.effort, fast: identity.fast, axes },
    });
  } catch (error) {
    // The pushed live choice (or the timeout) reconciles the display back to truth.
    console.warn(`Failed to set model for agent ${agentId}`, error);
  }
}

function schedulePendingTimeout(agentId: string, identity: ModelIdentity): void {
  setTimeout(() => {
    const pending = pendingByAgent.get(agentId);
    if (pending && pickIdentityEquals(pending.identity, identity)) {
      pendingByAgent.delete(agentId);
      m.redraw();
    }
  }, PENDING_TIMEOUT_MS);
}

/** The agent's current fast state, from its effective (live or pending) choice;
 *  false when the agent's model is not resolved. Used by the workspace fast-mode
 *  prompt to decide whether the question is still open. */
export function getAgentFastMode(agentId: string): boolean {
  const agent = getAgentById(agentId);
  const choice = effectiveChoice(agentId, agent?.model_choice);
  return choice?.identity.fast ?? false;
}

/** Turn fast mode on/off on the agent's current model, keeping model and effort.
 *  Used by the workspace fast-mode prompt. No-op when the agent's model is unknown
 *  or does not support fast mode. */
export function setFastMode(agentId: string, enabled: boolean): void {
  const agent = getAgentById(agentId);
  const choice = effectiveChoice(agentId, agent?.model_choice);
  if (!choice || choice.matched === null || !choice.matched.supports_fast) {
    return;
  }
  // Diff against the matched option's catalog id (choice.identity.model_id is the raw
  // reported id), so only the fast axis is sent -- not a spurious /model.
  const prev = { model_id: choice.matched.id, effort: choice.identity.effort, fast: choice.identity.fast };
  const next = { model_id: choice.matched.id, effort: choice.identity.effort, fast: enabled };
  setModelChoice(agentId, next, choice.matched, changedAxes(prev, next));
}
