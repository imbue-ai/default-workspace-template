// First-run splash. Simplified from the legacy three-choice account gate: the
// separate auth project owns sign-up/sign-in now, so this greets the user and
// offers one clear way forward -- get started building a machine. Choosing it
// records the continue-without-account intent so the first-run routing stops
// returning here. Self-advances to home the moment an account appears (e.g. a
// sign-in that completed elsewhere): every channel `accounts` message triggers
// a redraw, so the onupdate hook re-checks the accounts store.

import m from "mithril";
import { getAppContext } from "../../app-context";
import { skipAccountSetup } from "../../models/onboarding";
import { Button } from "../components/Button";

function WelcomePageComponent(): m.Component {
  let isBusy = false;
  let hasAdvanced = false;

  async function getStarted(): Promise<void> {
    isBusy = true;
    m.redraw();
    await skipAccountSetup();
    m.route.set("/create");
  }

  // A sign-in can complete without this page navigating (OAuth finishing in
  // an external browser); advance once the account list reports one. Every
  // channel `accounts` message triggers a redraw, so onupdate re-checks --
  // the navigation lives in lifecycle hooks to keep view() pure.
  function advanceIfSignedIn(): void {
    if (!hasAdvanced && getAppContext().stores.accounts.hasAccounts) {
      hasAdvanced = true;
      m.route.set("/");
    }
  }

  return {
    oninit: advanceIfSignedIn,
    onupdate: advanceIfSignedIn,
    view() {
      return m("div", { class: "min-h-full flex items-center justify-center" }, [
        m("div", { class: "max-w-sm w-full px-6 text-center" }, [
          m("h1", { class: "type-heading-lg text-primary mb-2" }, "Welcome to Minds"),
          m("p", { class: "text-secondary type-body mb-8" }, "Run persistent, autonomous AI agents."),
          m(
            Button,
            {
              variant: "primary",
              size: "lg",
              block: true,
              id: "welcome-get-started-btn",
              disabled: isBusy,
              onclick: () => void getStarted(),
            },
            "Get started",
          ),
        ]),
      ]);
    },
  };
}

export const WelcomePage: m.ComponentTypes = WelcomePageComponent;
