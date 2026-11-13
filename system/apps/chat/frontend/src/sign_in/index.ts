/**
 * The early sign-in page: the provider chooser on its own, served by `chat-early-sign-in`
 * (imbue/chat/early_sign_in.py) while the workspace is built, and framed by the Imbue Studio
 * app's creation page. It is the same chooser the workspace opens, over the same routes; the
 * page only tells the program when the user is done with it.
 */

import m from "mithril";
import "../style.css";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { closeProviderChooser, isProviderChooserOpen, openProviderChooser } from "../models/Providers";
import { ProviderChooserModal } from "../views/ProviderChooserModal";

/** Tell the program the user is done with the chooser: they closed it, or signed in. */
export function reportDone(): void {
  void m.request({ method: "POST", url: apiUrl("/api/early-sign-in/done") }).catch(() => undefined);
}

export const SignInPage: m.Component = {
  view() {
    return isProviderChooserOpen()
      ? m(ProviderChooserModal, {
          onDismiss: () => {
            closeProviderChooser();
            reportDone();
          },
        })
      : null;
  },
};

const rootElement = document.getElementById("app");
if (rootElement !== null) {
  // A signed-in account the user picks counts as done too, which is what makes those rows pickable.
  openProviderChooser({ onSignedIn: () => reportDone() });
  m.mount(rootElement, SignInPage);
}
