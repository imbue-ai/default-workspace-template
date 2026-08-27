/**
 * The static, per-harness model catalog -- the model bar's compile-time half.
 *
 * Fetched once from `GET /api/harnesses` and cached by harness. Holds everything
 * the bar needs that does not vary per agent: the selectable models (and which
 * efforts each declares, which are shown) and the switch mode. The per-agent live
 * selection arrives separately, on the agents WebSocket as each agent's
 * `model_choice` (see AgentManager.ts). The backend already computes which catalog
 * option a live choice matched, so the frontend never re-matches. The provider a chat runs on
 * is not here either -- the combo card reads it from the chat's own account label.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

export interface EffortChoice {
  level: string;
  in_picker: boolean;
}

export interface CatalogModelOption {
  id: string;
  label: string;
  efforts: EffortChoice[];
  supports_fast: boolean;
  in_picker: boolean;
  // The raw model id the harness reports in its live state file (or null = same as `id`).
  // Matched on the backend; the frontend reconciles against the matched option, not this.
  harness_reported_model_id: string | null;
}

// A popup the harness declared for the chat UI (see HarnessSpec.popups on the
// backend). `composer_command` popups match a typed message's first token against
// `commands` at send time; the `turn_check` popup is the fast-mode grace-period
// check ChatPanel runs per render. The frontend acts on whatever the agent's
// harness declared -- it never branches on the harness name.
export interface HarnessPopup {
  trigger: "composer_command" | "turn_check";
  commands: string[];
  action: "notice" | "open_auth" | "fast_mode_prompt";
  /** `notice` only: replaces the notice's default body. Absent for most declines,
   *  which are declined for the same reason (the command takes over the terminal);
   *  present where the harness has a more specific thing to say. */
  notice_body?: string | null;
}

export interface HarnessCatalog {
  // The static catalog options. EMPTY for a "dynamic" picker (codex): its options are per-agent,
  // fetched from /model-options on open, not carried here.
  options: CatalogModelOption[];
  // "eager_then_reconcile" (claude/pi -- optimistic) | "on_change" (codex -- no overlay)
  // | "read_only" (antigravity -- slots render non-interactive, no picker)
  switch_mode: string;
  picker_mode: string; // "list" | "search" | "dynamic" -- how the model dropdown sources/renders options
  // Whether the "Shoulder tap" button can flush the queue atomically (merge into the live
  // turn without a restart). Every current harness supports it (claude via its cancel chord,
  // pi via its inbox sentinel, codex via its live ledger); a false harness would fall back
  // to the restart-based flush.
  native_atomic_shoulder_tap_possible: boolean;
  // The harness's declared popups plus its agent-auth surface, merged into the
  // payload from the backend HarnessSpec. Optional so a stale backend without
  // them degrades to "no popups" rather than a parse failure.
  popups?: HarnessPopup[];
}

const catalogByHarness = new Map<string, HarnessCatalog>();
// Single-flight load: the in-flight (or completed) fetch, or null before the first
// attempt and after a failure -- so callers can genuinely await the load and a
// failed fetch is retried by the next caller rather than wedging the session.
let loadPromise: Promise<void> | null = null;

/** The catalog for a harness, or null when unknown / not yet loaded. */
export function getHarnessCatalog(harness: string | undefined): HarnessCatalog | null {
  if (!harness) {
    return null;
  }
  return catalogByHarness.get(harness) ?? null;
}

/** Load the catalogs once (single-flight). Awaiting this while a load is in
 *  flight resolves when THAT load lands, so a caller that needs the data (the
 *  composer's slash-command guard) can block on it. Safe to call on every
 *  agent switch. */
export function ensureHarnessCatalogs(): Promise<void> {
  if (loadPromise === null) {
    loadPromise = m
      .request<Record<string, HarnessCatalog>>({
        method: "GET",
        url: apiUrl("/api/harnesses"),
      })
      .then((data) => {
        for (const [harness, catalog] of Object.entries(data)) {
          catalogByHarness.set(harness, catalog);
        }
        m.redraw();
      })
      .catch((error) => {
        console.warn("Failed to load harness catalogs", error);
        loadPromise = null;
      });
  }
  return loadPromise;
}

/** The composer_command popup of `harness` matching `text`'s first token, or null.
 *  Matched on the command name (first whitespace token, lowercased, exact), so
 *  every argument form matches with its command -- deliberately over-matching,
 *  because a declined form that would have worked costs one trip to the terminal
 *  while an allowed one can take over the agent's pane or run the wrong auth flow. */
export function findComposerPopup(
  harness: string | undefined,
  text: string,
): { popup: HarnessPopup; command: string } | null {
  const firstToken = text.trim().toLowerCase().split(/\s+/, 1)[0] ?? "";
  if (!firstToken.startsWith("/")) {
    return null;
  }
  for (const popup of getHarnessCatalog(harness)?.popups ?? []) {
    if (popup.trigger === "composer_command" && popup.commands.includes(firstToken)) {
      return { popup, command: firstToken };
    }
  }
  return null;
}

/** Whether `harness` declared the fast-mode grace-period prompt. */
export function hasFastModePrompt(harness: string | undefined): boolean {
  return (getHarnessCatalog(harness)?.popups ?? []).some(
    (popup) => popup.trigger === "turn_check" && popup.action === "fast_mode_prompt",
  );
}
