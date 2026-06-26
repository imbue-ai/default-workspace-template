/**
 * Modal dialog for creating a new browser in the per-workspace fleet.
 * Mirrors CreateAgentModal: a single "Browser Name" input pre-filled with a
 * random name that the user can edit.
 *
 * Browsers are addressed by NAME everywhere (not a numeric id): the CLI
 * ``<name>`` arg, the ``service:browser?session=<name>`` ref, the cast
 * WebSocket ``/browsers/<name>/cast``, the manifest, and the on-disk profile
 * dir all key off the chosen name. The user may type any valid name (lowercase
 * alnum words joined by single dashes); the daemon rejects invalid names (400)
 * and duplicates / a full fleet (409), and this modal surfaces the daemon's
 * error verbatim inline rather than alerting.
 *
 * Duplicate-name guard (two layers): a typed name that already names a live
 * browser must NOT reach the optimistic-open path, because opening the pane for
 * an existing name would dedup onto that browser's pane and a subsequent 409
 * teardown would then close the EXISTING healthy pane. Layer one: this modal
 * pre-validates the typed name against ``existingBrowserNames`` and shows an
 * inline error without opening a pane or calling create. Layer two (defense in
 * depth, in the parent): ``onAccept`` reports whether it actually created a new
 * pane, and ``onFailed`` only tears the pane down when this flow created it.
 *
 * Close-immediately + optimistic 'starting' pane: the daemon now REGISTERS the
 * browser instantly (the Chromium launch runs serialized in the background and
 * the viewer watches it flip from ``init`` to ``running`` over the cast socket),
 * so the create POST returns fast. The instant the user confirms a non-empty,
 * non-duplicate name this modal:
 *   1. opens the optimistic pane via ``onAccept(name)`` (the viewer shows the full
 *      "Starting browser…" overlay until the daemon broadcasts ``running``), and
 *   2. CLOSES the modal immediately (the parent's ``onAccept`` clears the flag) --
 *      it does NOT wait for the POST.
 * The POST then runs in the background:
 *   - on success it calls ``onCreated(finalName)`` (the user always typed/accepted
 *     a name here, so it matches the already-open pane) to refresh the fleet list;
 *   - on failure (400 invalid / 409 duplicate-or-full / 503 installing / network)
 *     it calls ``onFailed(name)`` so the parent tears down the optimistic pane
 *     (only when this flow created it). Because the modal is already closed, the
 *     failure surfaces by the pane disappearing rather than an inline message.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

interface CreateBrowserModalAttrs {
  // Service base URL for the browser daemon (``/service/browser/``). Passed in
  // so the modal does not need to import the workspace's service-URL helper.
  browserServiceUrl: string;
  // Names of the browsers already in the fleet (the same list that drives the
  // "active browser" dropdown). Used to pre-validate a typed name: a duplicate
  // is rejected inline before any pane is opened or any create is attempted.
  existingBrowserNames: string[];
  // Fired the instant the user accepts a non-empty name, BEFORE the POST
  // resolves, so the parent can open the optimistic 'starting' pane keyed by
  // this name. Returns ``true`` when a NEW pane was created, ``false`` when the
  // open deduped onto a pane that was already showing this browser -- the modal
  // forwards this to ``onFailed`` so a failure never closes a pre-existing pane.
  onAccept: (browserName: string) => boolean;
  // Fired after the create POST succeeds (the launch completed server-side).
  // Carries the daemon's final chosen name (equal to the accepted name, since
  // the user always supplies one here).
  onCreated: (browserName: string) => void;
  // Fired when the create POST fails (400 invalid / 409 duplicate-or-full /
  // 503 still installing). ``createdPane`` echoes the ``onAccept`` return so the
  // parent only closes the optimistic pane when this flow actually created it.
  onFailed: (browserName: string, createdPane: boolean) => void;
  onCancel: () => void;
}

export function CreateBrowserModal(): m.Component<CreateBrowserModalAttrs> {
  let name = "";
  let loading = false;
  let error: string | null = null;

  async function fetchRandomName(): Promise<void> {
    try {
      const response = await m.request<{ name: string }>({
        method: "GET",
        url: apiUrl("/api/random-name"),
      });
      name = response.name;
      m.redraw();
    } catch {
      name = `browser-${Date.now().toString(36)}`;
    }
  }

  async function submit(attrs: CreateBrowserModalAttrs): Promise<void> {
    const chosen = name.trim();
    if (!chosen || loading) {
      return;
    }

    // Layer one (pre-validate): if the typed name already names a live browser,
    // reject it inline. Crucially this happens BEFORE ``onAccept`` -- opening
    // the pane for an existing name would dedup onto that browser's healthy
    // pane, and the daemon's 409 would then tear it down. By stopping here we
    // never open a pane or call create for a duplicate. (The daemon still
    // enforces uniqueness authoritatively; this is just a fast, safe guard.)
    if (attrs.existingBrowserNames.includes(chosen)) {
      error = `A browser named ${chosen} already exists`;
      m.redraw();
      return;
    }

    loading = true;
    error = null;

    // Open the optimistic pane, then close the modal IMMEDIATELY -- we do not wait
    // for the POST. The pane shows the full "Starting browser…" overlay and flips to
    // the live page on its own when the daemon broadcasts ``running``. ``createdPane``
    // records whether this actually created a new pane so a later failure only closes
    // one this flow owns. ``onAccept`` (in the parent) also clears the modal flag.
    const createdPane = attrs.onAccept(chosen);

    // Background POST: registers the browser server-side (returns fast) and kicks off
    // the serialized launch. The modal is already gone, so success just refreshes the
    // fleet list and failure tears the optimistic pane back down.
    void (async () => {
      let response: globalThis.Response;
      try {
        response = await fetch(`${attrs.browserServiceUrl}browsers`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: chosen }),
        });
      } catch {
        attrs.onFailed(chosen, createdPane);
        return;
      }
      const data = (await response.json().catch(() => ({}))) as { name?: string; error?: string };
      if (response.ok) {
        attrs.onCreated(typeof data.name === "string" ? data.name : chosen);
        return;
      }
      // 400 invalid / 409 duplicate-or-full / 503 installing: the registration was
      // rejected, so tear down the optimistic pane (only if this flow created it).
      attrs.onFailed(chosen, createdPane);
    })();
  }

  return {
    oninit() {
      fetchRandomName();
    },

    view(vnode) {
      const attrs = vnode.attrs;

      return m(
        "div.custom-url-dialog-overlay",
        {
          onclick(e: MouseEvent) {
            if ((e.target as HTMLElement).classList.contains("custom-url-dialog-overlay")) {
              attrs.onCancel();
            }
          },
        },
        [
          m(
            "div.custom-url-dialog",
            {
              onclick(e: MouseEvent) {
                e.stopPropagation();
              },
            },
            [
              m("h3.custom-url-dialog-title", "New browser"),
              m("label.custom-url-dialog-label", "Browser Name"),
              m("input.custom-url-dialog-input", {
                type: "text",
                value: name,
                placeholder: "browser-name",
                autofocus: true,
                oninput(e: InputEvent) {
                  name = (e.target as HTMLInputElement).value;
                },
                onkeydown(e: KeyboardEvent) {
                  if (e.key === "Enter") {
                    submit(attrs);
                  }
                  if (e.key === "Escape") {
                    attrs.onCancel();
                  }
                },
              }),
              error ? m("p", { style: "color: red; font-size: 0.85em; margin-top: 4px;" }, error) : null,
              m("div.custom-url-dialog-actions", [
                m(
                  "button.custom-url-dialog-cancel",
                  {
                    onclick: attrs.onCancel,
                    disabled: loading,
                  },
                  "Cancel",
                ),
                m(
                  "button.custom-url-dialog-open",
                  {
                    onclick: () => submit(attrs),
                    disabled: loading || !name.trim(),
                  },
                  loading ? "Starting..." : "Create",
                ),
              ]),
            ],
          ),
        ],
      );
    },
  };
}
