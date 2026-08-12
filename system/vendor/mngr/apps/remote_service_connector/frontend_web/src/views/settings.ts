// Settings: master-password management (change, clear) and the
// remember-on-this-device toggle's escape hatch (forget the local key).

import m from "mithril";
import { fetchKeyBundle } from "../api";
import { WrongPasswordOrCorruptDataError } from "../crypto/secretbox";
import {
  changePassword,
  clearPasswordAndSecrets,
  forgetDek,
} from "../dekstore";
import { refreshGate } from "./shell";

export function SettingsView(): m.Component {
  let oldPassword = "";
  let newPassword = "";
  let message = "";
  let error = "";
  let busy = false;

  async function submitChange(): Promise<void> {
    busy = true;
    message = "";
    error = "";
    m.redraw();
    try {
      const bundle = await fetchKeyBundle();
      if (bundle === null) {
        error = "No master password is set yet; unlock the account first.";
        return;
      }
      if (newPassword.length < 8) {
        error = "Choose a new password of at least 8 characters.";
        return;
      }
      await changePassword(bundle, oldPassword, newPassword, false);
      message = "Master password changed.";
      oldPassword = "";
      newPassword = "";
    } catch (changeError) {
      error =
        changeError instanceof WrongPasswordOrCorruptDataError
          ? "The current password is wrong."
          : `Change failed: ${String(changeError)}`;
    } finally {
      busy = false;
      m.redraw();
    }
  }

  async function submitClear(): Promise<void> {
    if (
      !window.confirm(
        "Clear the master password? Synced workspace secrets will be scrubbed " +
          "and every device will need workspace keys re-established.",
      )
    ) {
      return;
    }
    busy = true;
    error = "";
    m.redraw();
    try {
      await clearPasswordAndSecrets();
      message = "Master password cleared.";
      await refreshGate();
    } catch (clearError) {
      error = `Clear failed: ${String(clearError)}`;
    } finally {
      busy = false;
      m.redraw();
    }
  }

  async function submitForget(): Promise<void> {
    await forgetDek();
    await refreshGate();
  }

  return {
    view() {
      return m(
        "div",
        { class: "p-6 max-w-lg space-y-8" },
        m("h1", { class: "text-xl font-semibold" }, "Settings"),
        m(
          "section",
          { class: "space-y-3" },
          m("h2", { class: "font-medium" }, "Change master password"),
          m("input", {
            class:
              "w-full rounded border border-slate-300 dark:border-slate-700 bg-transparent px-3 py-2",
            type: "password",
            placeholder: "Current password",
            value: oldPassword,
            oninput(event: InputEvent) {
              oldPassword = (event.target as HTMLInputElement).value;
            },
          }),
          m("input", {
            class:
              "w-full rounded border border-slate-300 dark:border-slate-700 bg-transparent px-3 py-2",
            type: "password",
            placeholder: "New password",
            value: newPassword,
            oninput(event: InputEvent) {
              newPassword = (event.target as HTMLInputElement).value;
            },
          }),
          m(
            "button",
            {
              class:
                "rounded bg-slate-900 dark:bg-slate-100 text-white dark:text-slate-900 px-4 py-2",
              disabled: busy,
              onclick: () => void submitChange(),
            },
            "Change password",
          ),
        ),
        m(
          "section",
          { class: "space-y-3" },
          m("h2", { class: "font-medium" }, "This device"),
          m(
            "button",
            {
              class:
                "rounded border border-slate-300 dark:border-slate-700 px-4 py-2",
              disabled: busy,
              onclick: () => void submitForget(),
            },
            "Lock now (forget the key on this device)",
          ),
        ),
        m(
          "section",
          { class: "space-y-3" },
          m("h2", { class: "font-medium text-red-600" }, "Danger zone"),
          m(
            "p",
            { class: "text-sm text-slate-500" },
            "Clearing the master password deletes the key bundle and scrubs synced workspace secrets.",
          ),
          m(
            "button",
            {
              class: "rounded border border-red-300 text-red-600 px-4 py-2",
              disabled: busy,
              onclick: () => void submitClear(),
            },
            "Clear master password",
          ),
        ),
        message ? m("p", { class: "text-sm text-emerald-600" }, message) : null,
        error ? m("p", { class: "text-sm text-red-600" }, error) : null,
      );
    },
  };
}
