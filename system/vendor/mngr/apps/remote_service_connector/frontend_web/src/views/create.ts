// The create flow view: a one-field form (workspace name) driving the
// browser-orchestrated create with visible step progress.

import m from "mithril";
import {
  type CreateProgress,
  CreateFlowError,
  runCreateFlow,
} from "../createflow";

export function CreateView(): m.Component {
  let displayName = "";
  let busy = false;
  let error = "";
  let progress: CreateProgress | null = null;

  async function submit(): Promise<void> {
    if (!displayName.trim()) {
      error = "Give the workspace a name.";
      return;
    }
    busy = true;
    error = "";
    m.redraw();
    try {
      const result = await runCreateFlow(displayName.trim(), (update) => {
        progress = update;
        m.redraw();
      });
      m.route.set(`/workspace/${result.hostId}`);
    } catch (createError) {
      error =
        createError instanceof CreateFlowError
          ? `Create failed at the ${createError.step} step: ${createError.message}`
          : `Create failed: ${String(createError)}`;
    } finally {
      busy = false;
      m.redraw();
    }
  }

  return {
    view() {
      return m(
        "div",
        { class: "p-6 max-w-lg space-y-4" },
        m("h1", { class: "text-xl font-semibold" }, "New workspace"),
        m(
          "p",
          { class: "text-sm text-slate-500" },
          "Creates a cloud workspace reachable from this browser. Region and size use this tier's defaults.",
        ),
        m(
          "form",
          {
            class: "space-y-4",
            onsubmit(event: Event) {
              event.preventDefault();
              void submit();
            },
          },
          m("input", {
            class:
              "w-full rounded border border-slate-300 dark:border-slate-700 bg-transparent px-3 py-2",
            placeholder: "Workspace name",
            autofocus: true,
            disabled: busy,
            value: displayName,
            oninput(event: InputEvent) {
              displayName = (event.target as HTMLInputElement).value;
            },
          }),
          m(
            "button",
            {
              class:
                "rounded bg-slate-900 dark:bg-slate-100 text-white dark:text-slate-900 px-4 py-2 font-medium",
              disabled: busy,
            },
            busy ? "Creating..." : "Create workspace",
          ),
        ),
        progress
          ? m(
              "div",
              {
                class:
                  "rounded border border-slate-200 dark:border-slate-800 p-4 text-sm",
              },
              m("p", { class: "font-medium" }, `Step: ${progress.step}`),
              m("p", { class: "text-slate-500" }, progress.message),
            )
          : null,
        error ? m("p", { class: "text-sm text-red-600" }, error) : null,
      );
    },
  };
}
