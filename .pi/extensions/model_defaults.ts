// Rationale and maintenance: system/changelog/submit-pi-current-startup-model.md

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

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

const MODEL_SUBSTITUTIONS: Readonly<Record<string, string>> = {
  "opencode/kimi-k2.6": "deepseek-v4.1-flash",
  "opencode-go/kimi-k2.6": "deepseek-v4.1-flash",
  "openrouter/moonshotai/kimi-k2.6": "deepseek/deepseek-v4.1-flash",
};

const STARTUP_REASON = "startup";

function substitutionFor(provider: string | undefined, modelId: string | undefined): string | undefined {
  if (!provider || !modelId) {
    return undefined;
  }
  return MODEL_SUBSTITUTIONS[`${provider}/${modelId}`];
}

export default function (pi: ExtensionAPI): void {
  pi.on("session_start", async (event: StartupEvent, ctx: StartupContext) => {
    try {
      if (event?.reason && event.reason !== STARTUP_REASON) {
        return;
      }
      const provider = ctx?.model?.provider;
      const replacementId = substitutionFor(provider, ctx?.model?.id);
      if (!provider || !replacementId) {
        return;
      }
      const registry = ctx.modelRegistry;
      const replacement = registry?.find?.(provider, replacementId);
      if (!replacement) {
        return;
      }
      if (registry?.hasConfiguredAuth && !registry.hasConfiguredAuth(replacement)) {
        return;
      }
      await pi.setModel(replacement);
    } catch {
      return;
    }
  });
}
