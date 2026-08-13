// The master-password unlock / first-time-setup panel (unlock-at-sign-in).

import m from "mithril";
import { fetchKeyBundle, KeyBundleExistsError } from "../api";
import {
  MalformedCiphertextError,
  WrongPasswordOrCorruptDataError,
} from "../crypto/secretbox";
import { setInitialPassword, unlockWithPassword } from "../dekstore";
import { markUnlocked } from "./shell";

// Placeholder until the Honest Software explainer page is published.
const HONEST_SOFTWARE_LEARN_MORE_URL = "#";

export function UnlockView(): m.Component {
  let password = "";
  let confirmPassword = "";
  let remember = false;
  let hasBundle: boolean | null = null;
  let error = "";
  let busy = false;

  void fetchKeyBundle()
    .then((bundle) => {
      hasBundle = bundle !== null;
      m.redraw();
    })
    .catch(() => {
      hasBundle = null;
      error = "Could not check the account's key state; reload to retry.";
      m.redraw();
    });

  async function submit(): Promise<void> {
    error = "";
    busy = true;
    m.redraw();
    try {
      if (hasBundle) {
        const outcome = await unlockWithPassword(password, remember);
        if (outcome === "no_bundle") {
          hasBundle = false;
          error = "No master password is set yet; create one below.";
          return;
        }
      } else {
        if (password.length < 8) {
          error = "Choose a master password of at least 8 characters.";
          return;
        }
        if (password !== confirmPassword) {
          error = "The passwords do not match.";
          return;
        }
        await setInitialPassword(password, remember);
      }
      markUnlocked();
    } catch (unlockError) {
      // Wrong password and damaged bundle are different problems with
      // different remedies: re-typing the password fixes the first and can
      // never fix the second, so they must not share a message.
      if (unlockError instanceof WrongPasswordOrCorruptDataError) {
        error = "Wrong master password.";
      } else if (unlockError instanceof KeyBundleExistsError) {
        // This tab lost the first-time-setup race (another tab or device
        // just set the master password); flip to the unlock form.
        hasBundle = true;
        error =
          "A master password was just set from another tab or device; " +
          "enter that password to unlock.";
      } else if (unlockError instanceof MalformedCiphertextError) {
        error =
          "Your stored key bundle appears to be damaged, so it cannot be " +
          "unlocked with any password. Clear the master password from " +
          "another signed-in device (or contact support) to set a new one.";
      } else {
        error = `Unlock failed: ${String(unlockError)}`;
      }
    } finally {
      busy = false;
      m.redraw();
    }
  }

  return {
    view() {
      const isSetup = hasBundle === false;
      return m(
        "div",
        { class: "flex grow items-center justify-center p-8" },
        m(
          "form",
          {
            class: "w-full max-w-sm space-y-4",
            onsubmit(event: Event) {
              event.preventDefault();
              void submit();
            },
          },
          m(
            "h1",
            { class: "text-xl font-semibold" },
            isSetup ? "Set a master password" : "Unlock your workspaces",
          ),
          isSetup
            ? m("p", { class: "text-sm text-slate-500 whitespace-pre-line" }, [
                'Minds is "Honest Software", which means that we cannot see your data.\n' +
                  "To ensure this, you must pick a master password that we don't know.\n" +
                  "Learn more here: ",
                // The Honest Software explainer does not exist yet; the link
                // target is filled in once it is published.
                m(
                  "a",
                  { class: "underline", href: HONEST_SOFTWARE_LEARN_MORE_URL },
                  "Honest Software",
                ),
              ])
            : m(
                "p",
                { class: "text-sm text-slate-500" },
                "Enter your master password to decrypt your workspace keys in this tab.",
              ),
          m("input", {
            class:
              "w-full rounded border border-slate-300 dark:border-slate-700 bg-transparent px-3 py-2",
            type: "password",
            placeholder: "Master password",
            autofocus: true,
            value: password,
            oninput(event: InputEvent) {
              password = (event.target as HTMLInputElement).value;
            },
          }),
          isSetup
            ? m("input", {
                class:
                  "w-full rounded border border-slate-300 dark:border-slate-700 bg-transparent px-3 py-2",
                type: "password",
                placeholder: "Confirm password",
                value: confirmPassword,
                oninput(event: InputEvent) {
                  confirmPassword = (event.target as HTMLInputElement).value;
                },
              })
            : null,
          m(
            "label",
            {
              class:
                "flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300",
            },
            m("input", {
              type: "checkbox",
              checked: remember,
              onchange(event: Event) {
                remember = (event.target as HTMLInputElement).checked;
              },
            }),
            "Remember on this device",
          ),
          error ? m("p", { class: "text-sm text-red-600" }, error) : null,
          m(
            "button",
            {
              class:
                "w-full rounded bg-slate-900 dark:bg-slate-100 text-white dark:text-slate-900 py-2 font-medium",
              disabled: busy || hasBundle === null,
            },
            busy ? "Working..." : isSetup ? "Set password" : "Unlock",
          ),
        ),
      );
    },
  };
}
