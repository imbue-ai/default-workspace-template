/**
 * The page: the header (the app's name on the overview, else the view's name with a way back, and a word when the
 * workspace stops answering), the view the path names, and the Stop question when one is up.
 *
 * The Tailwind scanner reads utility names from the literals in this file: keep every utility name a contiguous
 * literal.
 */

import m from "mithril";

import { buttonClass, buttonPartAttrs } from "@imbue/workspace-ui/src/components/Button";
import { makeNoticeDialog } from "@imbue/workspace-ui/src/components/NoticeDialog";
import { icon } from "@imbue/workspace-ui/src/components/icons";

import type { StopTarget } from "../models/api";
import { STOP_WORDS } from "../models/format";
import { APP_TITLE, VIEW_TITLES, type MonitorModel, type StopQuestion } from "../models/monitor";
import { DiskView } from "./DiskView";
import { Overview } from "./Overview";
import { CpuView, MemoryView } from "./ResourceViews";
import { dot } from "./parts";

export interface AppAttrs {
  model: MonitorModel;
}

const StopDialog = makeNoticeDialog();

function header(model: MonitorModel): m.Vnode {
  const title = "type-title-page m-0 text-body-content";
  return m("header", { class: "mb-3 flex min-h-7 items-center justify-between gap-3" }, [
    m(
      "div",
      { class: "flex items-center gap-2" },
      model.view === "overview"
        ? m("h1", { class: title }, APP_TITLE)
        : [
            m(
              "a",
              {
                href: "/",
                "aria-label": "Back to the overview",
                class: buttonClass("ghost", { sm: true, extra: "-ml-2 no-underline" }),
                ...buttonPartAttrs("ghost", false),
              },
              [m.trust(icon("chevron-left", { size: 14 })), APP_TITLE],
            ),
            m("h1", { class: title }, VIEW_TITLES[model.view]),
          ],
    ),
    model.isOffline
      ? m("span", { class: "offline flex items-center gap-1.5 type-helper text-danger-surface", role: "status" }, [
          dot("bad"),
          "Can't reach the workspace, retrying",
        ])
      : null,
  ]);
}

function stopDialog(model: MonitorModel, question: StopQuestion): m.Children {
  const words = STOP_WORDS[question.target.kind];
  if (question.phase === "failed") {
    return m(StopDialog, {
      title: `Couldn't ${words.verb.toLowerCase()} ${question.name}`,
      body: [question.failure],
      dismissLabel: "OK",
      onDismiss: () => model.dismissStop(),
    });
  }
  const isStopping = question.phase === "stopping";
  return m(StopDialog, {
    title: words.ask(question.name),
    body: [words.then],
    dismissLabel: "Cancel",
    isDismissable: !isStopping,
    actions: [
      {
        label: isStopping ? words.busy : words.verb,
        isDestructive: true,
        isDisabled: isStopping,
        run: () => void model.confirmStop(),
      },
    ],
    onDismiss: () => model.dismissStop(),
  });
}

export const App: m.Component<AppAttrs> = {
  view({ attrs: { model } }) {
    const onStop = (target: StopTarget, name: string): void => model.askToStop(target, name);
    let body: m.Children;
    switch (model.view) {
      case "overview":
        body = m(Overview, { stats: model.stats, onStop });
        break;
      case "cpu":
        body = m(CpuView, { model, onStop });
        break;
      case "memory":
        body = m(MemoryView, { model, onStop });
        break;
      case "disk":
        body = m(DiskView, {
          disk: model.disk,
          isScanRequested: model.isScanRequested,
          onScan: () => void model.scanDisk(),
        });
        break;
    }
    return [
      // The shared base style pins the body to the window with its overflow hidden, so the page scrolls here.
      m(
        "main",
        { class: "h-screen overflow-y-auto" },
        m("div", { class: "mx-auto max-w-[640px] px-6 pt-5 pb-10 max-[480px]:px-4" }, [header(model), body]),
      ),
      model.stopQuestion === null ? null : stopDialog(model, model.stopQuestion),
    ];
  },
};
