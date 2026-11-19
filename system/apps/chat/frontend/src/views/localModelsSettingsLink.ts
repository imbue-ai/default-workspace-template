/**
 * The link a "no model server" message carries into Imbue Studio's Settings, at the Local models
 * section, where Studio installs Ollama. The message beside it names the section too, because a
 * chat opened outside Imbue Studio has nothing to open.
 */

import m from "mithril";
import { OPEN_LOCAL_MODELS_SETTINGS, sendToEmbedder } from "@imbue/workspace-ui/src/embed";
import { HINT_ACTION } from "./providerSignInStyles";

export const LOCAL_MODELS_SETTINGS_LINK_LABEL = "Open Local models settings";

export function openLocalModelsSettings(): void {
  sendToEmbedder(OPEN_LOCAL_MODELS_SETTINGS);
}

export function localModelsSettingsLink(): m.Children {
  return m(
    "button",
    {
      type: "button",
      class: HINT_ACTION,
      "data-e2e": "open-local-models-settings",
      onclick: openLocalModelsSettings,
    },
    LOCAL_MODELS_SETTINGS_LINK_LABEL,
  );
}
