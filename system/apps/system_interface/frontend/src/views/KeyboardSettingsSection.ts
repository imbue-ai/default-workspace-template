/**
 * The body of the Keyboard shortcuts dialog (`KeyboardShortcutsDialog.ts`, opened from the
 * backdrop's context menu; user-profile-state spec, section 10.3): every app's actions from
 * their manifests, a chord recorder, and the save surfaces. The owner
 * chooses "only in this workspace" (the override file) or "everywhere" (the account, through the
 * delivery flow); a visitor's save is always their own override here. An "everywhere" save the
 * account has not taken yet shows as waiting, with retry and discard.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { chordFromKeydown } from "@imbue/workspace-ui/src/keybindings";
import {
  type KeybindingActionView,
  type KeybindingAppView,
  type KeybindingsCatalog,
  type KeybindingsDeliveryOutcome,
  deliverKeybindings,
  discardKeybinding,
  fetchKeybindingsCatalog,
  resetKeybinding,
  saveKeybinding,
} from "../model/api";
import type { FailureNotice } from "../model/failures";
import { FailureLine } from "./FailureLine";
import type { ReportBug } from "./FailureLine";

export interface KeyboardSettingsSectionAttrs {
  /** A failed read or change as the section's line says it, from what failed (a sentence stem) and the error, with
   *  ``retry`` where the failure is one to try again. */
  readonly wordFailure: (whatFailed: string, error: unknown, retry: () => void) => FailureNotice;
  /** Report a bug about a failure where it asks for one; null where no bug report can be opened. */
  readonly onReportBug: ReportBug;
}

/** What a chord list reads as on screen. */
export function chordsLabel(chords: readonly string[]): string {
  if (chords.length === 0) return "not bound";
  return chords.join(", ");
}

/** The other action of the same app already using the chord, if any (conflicts are per app). */
export function conflictingAction(
  app: KeybindingAppView,
  actionId: string,
  chord: string,
): KeybindingActionView | null {
  return app.actions.find((action) => action.id !== actionId && action.chords.includes(chord)) ?? null;
}

const SHORTCUTS_NOT_LOADED = "Your keyboard shortcuts couldn't be loaded";
const SHORTCUT_NOT_SAVED = "This shortcut couldn't be saved";
const SHORTCUT_NOT_RESET = "This shortcut couldn't be reset";
const UNSENT_NOT_DISCARDED = "This change couldn't be discarded";
const UNSENT_NOT_DELIVERED = "Your shortcuts couldn't be saved to your account";

function failureLine(attrs: KeyboardSettingsSectionAttrs, notice: FailureNotice): m.Children {
  return m(FailureLine, {
    notice,
    onReportBug: attrs.onReportBug,
    marker: "keyboard-settings-failure",
    messageClass: "type-helper text-danger",
  });
}

const SOURCE_BADGES: Record<string, string> = {
  workspace: "this workspace",
  unsent: "waiting to reach your account",
  account: "your account",
};

export function KeyboardSettingsSection(): m.Component<KeyboardSettingsSectionAttrs> {
  let catalog: KeybindingsCatalog | null = null;
  let loadError: FailureNotice | null = null;
  // The one action being re-bound, and the chord its recorder captured so far.
  let recording: { app: string; action: string } | null = null;
  let recordedChord: string | null = null;
  let conflict: string | null = null;
  // What the last delivery attempt said of why the saves did not reach the account.
  let notice: string | null = null;
  // Why the last save, reset, discard or delivery retry failed.
  let failure: FailureNotice | null = null;
  let isBusy = false;

  async function load(attrs: KeyboardSettingsSectionAttrs): Promise<void> {
    try {
      catalog = await fetchKeybindingsCatalog();
      loadError = null;
    } catch (error) {
      loadError = attrs.wordFailure(SHORTCUTS_NOT_LOADED, error, () => void load(attrs));
    }
    m.redraw();
  }

  function startRecording(app: string, action: string): void {
    recording = { app, action };
    recordedChord = null;
    conflict = null;
    notice = null;
    failure = null;
  }

  function stopRecording(): void {
    recording = null;
    recordedChord = null;
    conflict = null;
  }

  function onRecorderKeydown(appView: KeybindingAppView, action: KeybindingActionView, event: KeyboardEvent): void {
    event.preventDefault();
    event.stopPropagation();
    const chord = chordFromKeydown(event);
    if (chord === null) return;
    recordedChord = chord;
    const taken = conflictingAction(appView, action.id, chord);
    conflict = taken === null ? null : `${chordsLabel([chord])} already runs "${taken.label}"`;
    m.redraw();
  }

  /** What a delivery attempt leaves on screen: nothing once it landed, else its own word on why not. */
  function noticeOfDelivery(delivery: KeybindingsDeliveryOutcome | null): string | null {
    return delivery === null || delivery.status === "DELIVERED" ? null : delivery.detail;
  }

  async function save(
    attrs: KeyboardSettingsSectionAttrs,
    app: string,
    action: string,
    scope: "workspace" | "account",
  ): Promise<void> {
    if (recordedChord === null || conflict !== null) return;
    isBusy = true;
    failure = null;
    try {
      const saved = await saveKeybinding(app, action, [recordedChord], scope);
      notice = noticeOfDelivery(saved.delivery);
      stopRecording();
      await load(attrs);
    } catch (error) {
      failure = attrs.wordFailure(SHORTCUT_NOT_SAVED, error, () => void save(attrs, app, action, scope));
    }
    isBusy = false;
    m.redraw();
  }

  /** Run a change (``whatFailed`` naming it, should it fail) and read the shortcuts again. */
  async function act(
    attrs: KeyboardSettingsSectionAttrs,
    whatFailed: string,
    run: () => Promise<unknown>,
  ): Promise<void> {
    isBusy = true;
    failure = null;
    try {
      await run();
      await load(attrs);
    } catch (error) {
      failure = attrs.wordFailure(whatFailed, error, () => void act(attrs, whatFailed, run));
    }
    isBusy = false;
    m.redraw();
  }

  function recorderRow(
    attrs: KeyboardSettingsSectionAttrs,
    appView: KeybindingAppView,
    action: KeybindingActionView,
  ): m.Children {
    const isOwner = catalog?.is_owner === true;
    const canEverywhere = catalog?.can_save_everywhere === true;
    return m("div", { class: "mb-2 rounded border border-default p-2", "data-keybinding-recorder": action.id }, [
      m("input", {
        class: "type-body w-full bg-transparent outline-none",
        readonly: true,
        placeholder: "Press the new shortcut...",
        value: recordedChord ?? "",
        oncreate: (created: m.VnodeDOM) => (created.dom as HTMLInputElement).focus(),
        onkeydown: (event: KeyboardEvent) => onRecorderKeydown(appView, action, event),
      }),
      conflict !== null ? m("p", { class: "type-helper mt-1 text-danger" }, conflict) : null,
      m("div", { class: "mt-2 flex flex-wrap gap-2" }, [
        m(
          Button,
          {
            sm: true,
            disabled: isBusy || recordedChord === null || conflict !== null,
            onclick: () => void save(attrs, appView.app, action.id, "workspace"),
          },
          isOwner ? "Only in this workspace" : "Save",
        ),
        isOwner
          ? m(
              Button,
              {
                sm: true,
                disabled: isBusy || recordedChord === null || conflict !== null || !canEverywhere,
                title: canEverywhere
                  ? undefined
                  : "Saving everywhere needs a connection to Imbue Studio, which this workspace does not have.",
                onclick: () => void save(attrs, appView.app, action.id, "account"),
              },
              "Everywhere",
            )
          : null,
        m(Button, { sm: true, variant: "ghost", onclick: stopRecording }, "Cancel"),
      ]),
      isOwner && !canEverywhere
        ? m(
            "p",
            { class: "type-helper mt-1 text-secondary" },
            "Shortcuts saved here apply only in this workspace: saving to your account needs Imbue Studio.",
          )
        : null,
    ]);
  }

  function actionRow(
    attrs: KeyboardSettingsSectionAttrs,
    appView: KeybindingAppView,
    action: KeybindingActionView,
  ): m.Children {
    const badge = SOURCE_BADGES[action.source];
    const isRecording = recording !== null && recording.app === appView.app && recording.action === action.id;
    return m("div", { class: "mb-1", "data-keybinding-action": `${appView.app}.${action.id}` }, [
      m("div", { class: "flex items-center justify-between gap-2" }, [
        m("span", { class: "type-body min-w-0 truncate" }, action.label),
        m("span", { class: "flex shrink-0 items-center gap-2" }, [
          m("span", { class: "type-helper text-secondary" }, chordsLabel(action.chords)),
          badge !== undefined ? m("span", { class: "type-helper text-secondary" }, `(${badge})`) : null,
          action.source === "workspace"
            ? m(
                Button,
                {
                  sm: true,
                  variant: "ghost",
                  disabled: isBusy,
                  onclick: () => void act(attrs, SHORTCUT_NOT_RESET, () => resetKeybinding(appView.app, action.id)),
                },
                "Reset",
              )
            : null,
          m(
            Button,
            { sm: true, variant: "ghost", disabled: isBusy, onclick: () => startRecording(appView.app, action.id) },
            "Change",
          ),
        ]),
      ]),
      isRecording ? recorderRow(attrs, appView, action) : null,
    ]);
  }

  function unsentRows(attrs: KeyboardSettingsSectionAttrs, loaded: KeybindingsCatalog): m.Children {
    if (loaded.unsent.length === 0) return null;
    return m("div", { class: "mt-2", "data-keybinding-unsent": true }, [
      m(
        "p",
        { class: "type-helper text-secondary" },
        loaded.has_pending_permission
          ? "Waiting for your approval in Imbue Studio; these apply in this workspace meanwhile."
          : "Not yet saved to your account; these apply in this workspace meanwhile.",
      ),
      ...loaded.unsent.map((entry) =>
        m("div", { class: "flex items-center justify-between gap-2" }, [
          m(
            "span",
            { class: "type-helper min-w-0 truncate" },
            `${entry.app}: ${entry.action} = ${chordsLabel(entry.chords)}`,
          ),
          m(
            Button,
            {
              sm: true,
              variant: "ghost",
              disabled: isBusy,
              onclick: () => void act(attrs, UNSENT_NOT_DISCARDED, () => discardKeybinding(entry.app, entry.action)),
            },
            "Discard",
          ),
        ]),
      ),
      m(
        Button,
        {
          sm: true,
          disabled: isBusy,
          onclick: () =>
            void act(attrs, UNSENT_NOT_DELIVERED, async () => {
              notice = noticeOfDelivery(await deliverKeybindings([]));
            }),
        },
        "Try saving to your account again",
      ),
    ]);
  }

  return {
    oninit(vnode) {
      void load(vnode.attrs);
    },
    view(vnode) {
      const attrs = vnode.attrs;
      if (loadError !== null) return failureLine(attrs, loadError);
      if (catalog === null) return m("p", { class: "type-helper text-secondary" }, "Loading the shortcuts...");
      const loaded = catalog;
      return m("div", { class: "mb-3", "data-keyboard-settings": true }, [
        ...loaded.apps
          .filter((appView) => appView.actions.length > 0)
          .map((appView) =>
            m("div", { class: "mb-2" }, [
              m("p", { class: "type-label text-primary" }, appView.display_name),
              ...appView.actions.map((action) => actionRow(attrs, appView, action)),
            ]),
          ),
        notice !== null ? m("p", { class: "type-helper mt-1 text-secondary" }, notice) : null,
        failure !== null ? m("div", { class: "mt-1" }, failureLine(attrs, failure)) : null,
        unsentRows(attrs, loaded),
      ]);
    },
  };
}
