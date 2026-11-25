// Replace a stale pi startup model with a current one, in place.
//
// WHY THIS EXISTS
//
// A new pi session with no saved default (`defaultProvider` / `defaultModel` in
// settings.json) gets its model from pi's own hardcoded table,
// `defaultModelPerProvider` in pi's `dist/core/model-resolver.js` -- one slug per
// provider, compiled into the binary. Nothing in the workspace can change it, and
// nothing warns when a slug in it stops being a good choice. On the OpenCode
// providers that table still says `kimi-k2.6`, so every pi chat signed into
// OpenCode Go or OpenCode Zen started on a deprecated model.
//
// `system/scripts/seed_home_skeleton.sh` only seeds the OpenRouter pair, because
// pi accepts exactly one saved default and it is ignored on any provider the user
// has not authenticated. That leaves the other providers on pi's table.
//
// So this extension runs pi's real resolution first and then corrects only the
// outcomes listed below. It is deliberately narrow:
//
//   * It acts only when the model pi chose is one of the deprecated slugs in the
//     table. A model the user picked (saved with Ctrl+S, or restored from a
//     resumed session) is not in the table, so it is never touched.
//   * A replacement stays on the SAME provider, so it can never need a credential
//     the user has not already supplied. If that provider turns out to be
//     unauthenticated, or the replacement slug is not in the catalog, nothing
//     happens and pi keeps what it chose.
//   * It runs on a fresh session only. Resuming keeps the session's own model.
//
// KEEPING IT CURRENT
//
// Each key is `provider/modelId` exactly as pi's `defaultModelPerProvider` spells
// it; each value is the model id to use on that same provider instead. pi's table
// is the thing that goes stale, so when a model here is itself deprecated, edit
// the one line. Entries are dropped (not rewritten) once pi's own default is no
// longer the deprecated slug.

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Structure-only views: this file must stay importable by a stub harness (the
// test in model_defaults_test.ts), so it depends on no pi runtime value.
interface StartupModel {
  provider?: string;
  id?: string;
}

interface StartupModelRegistry {
  find?: (provider: string, modelId: string) => StartupModel | undefined;
  hasConfiguredAuth?: (model: StartupModel) => boolean;
}

interface StartupContext {
  model?: StartupModel;
  modelRegistry?: StartupModelRegistry;
}

interface StartupEvent {
  reason?: string;
}

/**
 * Deprecated startup models -> the model to start on instead, on the same provider.
 *
 * `opencode` and `opencode-go` are OpenCode Zen and OpenCode Go; both are what a
 * chat signed into OpenCode lands on. `openrouter` is only reached when the seeded
 * OpenRouter default is missing (a settings file the user cleared by hand), and its
 * ids carry their own vendor prefix.
 */
export const MODEL_SUBSTITUTIONS: Readonly<Record<string, string>> = {
  "opencode/kimi-k2.6": "deepseek-v4.1-flash",
  "opencode-go/kimi-k2.6": "deepseek-v4.1-flash",
  "openrouter/moonshotai/kimi-k2.6": "deepseek/deepseek-v4.1-flash",
};

// Session starts that represent a fresh selection rather than a continuation.
const FRESH_START_REASONS: ReadonlySet<string> = new Set(["startup", "new"]);

/**
 * The model id `provider/modelId` should start on instead of the given startup
 * model, or undefined when the model is fine as chosen.
 *
 * Split out from the handler so it can be exercised without a pi process.
 */
export function substitutionFor(provider: string | undefined, modelId: string | undefined): string | undefined {
  if (!provider || !modelId) {
    return undefined;
  }
  return MODEL_SUBSTITUTIONS[`${provider}/${modelId}`];
}

export default function (pi: ExtensionAPI): void {
  pi.on("session_start", async (event: StartupEvent, ctx: StartupContext) => {
    try {
      if (event?.reason && !FRESH_START_REASONS.has(event.reason)) {
        return;
      }
      const provider = ctx?.model?.provider;
      const replacementId = substitutionFor(provider, ctx?.model?.id);
      if (!provider || !replacementId) {
        return;
      }
      const registry = ctx.modelRegistry;
      const replacement = registry?.find?.(provider, replacementId);
      // A model the catalog no longer carries, or one whose provider has no
      // credential, is not this extension's business: leave pi's choice alone
      // rather than failing a startup over it.
      if (!replacement) {
        return;
      }
      if (registry?.hasConfiguredAuth && !registry.hasConfiguredAuth(replacement)) {
        return;
      }
      await pi.setModel(replacement);
    } catch {
      // Never break a session start over a model preference.
    }
  });
}
