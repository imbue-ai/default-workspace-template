/**
 * The page's state: which page the path names, what the API answered for it, the polling, the schedule
 * editor, and the toasts a failed action is reported with.
 */

import m from "mithril";

import { Toasts } from "@imbue/workspace-ui/src/components/toasts";
import { ToastQueue } from "@imbue/workspace-ui/src/models/toasts";

import {
  fetchDetail,
  fetchListing,
  fetchLog,
  fetchRun,
  openFixChat,
  removeRoutine,
  runNow,
  setEnabled,
  setSchedule,
  type RoutineDetail,
  type RoutineListing,
  type RunDetail,
  type RunRecord,
} from "../models/api";
import { pageOf, pathOf, titleOf, type Page } from "../models/routing";
import { changeOf, formOf, type ScheduleForm } from "../models/schedule";
import { ListPage } from "./ListPage";
import { LogPage } from "./LogPage";
import { RoutinePage } from "./RoutinePage";
import { RunPage } from "./RunPage";
import { ScheduleDialog } from "./ScheduleDialog";

/** How often the page re-reads: faster while a run is going, since that is when it is being watched. */
export const IDLE_REFRESH_MS = 30_000;
export const RUNNING_REFRESH_MS = 5_000;

export interface AppHost {
  /** Tell the shell where the page is and what it is called. */
  reportLocation: (path: string, title: string) => void;
  /** Start a chat with the text (the list's add field). */
  startWithText: (text: string) => void;
  /** Ask the user to confirm removing a routine. */
  confirmRemove: (title: string) => Promise<boolean>;
  navigate: (path: string) => void;
}

type Loaded =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "list"; listing: RoutineListing }
  | { kind: "routine"; detail: RoutineDetail }
  | { kind: "run"; detail: RunDetail; routineTitle: string }
  | { kind: "log"; run: RunRecord; log: string; routineTitle: string };

export class AppModel {
  readonly page: Page;
  loaded: Loaded = { kind: "loading" };
  scheduleForm: ScheduleForm | null = null;
  /** Whether the Remove confirmation is up, which holds the polling as the editor does. */
  isConfirmingRemoval = false;
  isBusy = false;
  readonly toasts: ToastQueue;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private isDisposed = false;

  constructor(
    pathname: string,
    private readonly host: AppHost,
    private readonly redraw: () => void,
  ) {
    this.page = pageOf(pathname);
    this.toasts = new ToastQueue(redraw);
    // The shell learns where the page is at once; the title is refined once the data names the routine.
    this.host.reportLocation(pathOf(this.page), titleOf(this.page, null));
  }

  async load(): Promise<void> {
    try {
      switch (this.page.kind) {
        case "list": {
          const listing = await fetchListing();
          this.loaded = { kind: "list", listing };
          break;
        }
        case "routine": {
          const detail = await fetchDetail(this.page.routineId);
          this.loaded = { kind: "routine", detail };
          this.host.reportLocation(pathOf(this.page), titleOf(this.page, detail.row.title));
          break;
        }
        case "run": {
          const [detail, routine] = await Promise.all([
            fetchRun(this.page.routineId, this.page.runId),
            fetchDetail(this.page.routineId),
          ]);
          this.loaded = { kind: "run", detail, routineTitle: routine.row.title };
          this.host.reportLocation(pathOf(this.page), titleOf(this.page, routine.row.title));
          break;
        }
        case "log": {
          const [detail, routine, log] = await Promise.all([
            fetchRun(this.page.routineId, this.page.runId),
            fetchDetail(this.page.routineId),
            fetchLog(this.page.routineId, this.page.runId),
          ]);
          this.loaded = { kind: "log", run: detail.run, log, routineTitle: routine.row.title };
          this.host.reportLocation(pathOf(this.page), titleOf(this.page, routine.row.title));
          break;
        }
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      // A page that has data keeps it: one refused re-read (the app restarting under the forwarder, a
      // tick's write racing the read) must not wipe what the user is looking at until the next poll.
      if (this.loaded.kind === "loading" || this.loaded.kind === "error") {
        this.loaded = { kind: "error", message };
      } else {
        this.toasts.show(`Couldn't refresh: ${message}`);
      }
    }
    this.redraw();
    this.scheduleRefresh();
  }

  /** Stop the polling for good: the page is leaving, or a test is done with the model. */
  dispose(): void {
    this.isDisposed = true;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }

  private scheduleRefresh(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    if (this.isDisposed) return;
    const isRunning =
      (this.loaded.kind === "list" && this.loaded.listing.summary.running > 0) ||
      (this.loaded.kind === "routine" && this.loaded.detail.row.is_running) ||
      (this.loaded.kind === "run" && this.loaded.detail.run.outcome === null) ||
      (this.loaded.kind === "log" && this.loaded.run.outcome === null);
    this.timer = setTimeout(
      () => {
        // Never redraw under an open dialog: that would throw away a half-made choice.
        if (this.scheduleForm !== null || this.isConfirmingRemoval) this.scheduleRefresh();
        else void this.load();
      },
      isRunning ? RUNNING_REFRESH_MS : IDLE_REFRESH_MS,
    );
  }

  /** Run the action with the page busy; a failure is reported as a toast. Answers whether it went through. */
  private async act(action: () => Promise<void>, failure: string): Promise<boolean> {
    this.isBusy = true;
    this.redraw();
    try {
      await action();
      return true;
    } catch (error) {
      this.toasts.show(`${failure}: ${error instanceof Error ? error.message : String(error)}`);
      return false;
    } finally {
      this.isBusy = false;
    }
  }

  private async actAndReload(action: () => Promise<void>, failure: string): Promise<void> {
    await this.act(action, failure);
    await this.load();
  }

  runNow(): void {
    if (this.page.kind !== "routine") return;
    const routineId = this.page.routineId;
    void this.actAndReload(async () => {
      await runNow(routineId);
    }, "Couldn't start it");
  }

  toggleEnabled(): void {
    if (this.page.kind !== "routine" || this.loaded.kind !== "routine") return;
    const routineId = this.page.routineId;
    const isEnabled = !this.loaded.detail.row.is_enabled;
    void this.actAndReload(async () => {
      await setEnabled(routineId, isEnabled);
    }, "Couldn't change it");
  }

  openScheduleEditor(): void {
    if (this.loaded.kind !== "routine" || this.loaded.detail.definition === null) return;
    this.scheduleForm = formOf(this.loaded.detail.definition);
  }

  closeScheduleEditor(): void {
    this.scheduleForm = null;
  }

  saveSchedule(): void {
    if (this.page.kind !== "routine" || this.scheduleForm === null) return;
    const routineId = this.page.routineId;
    const change = changeOf(this.scheduleForm);
    void this.actAndReload(async () => {
      await setSchedule(routineId, change);
      this.scheduleForm = null;
    }, "Couldn't change the schedule");
  }

  fix(): void {
    if (this.page.kind !== "routine") return;
    const routineId = this.page.routineId;
    void this.actAndReload(async () => {
      await openFixChat(routineId);
      this.toasts.show("A chat is opening with everything the agent needs to start.");
    }, "Couldn't start a chat");
  }

  /** Remove the routine and leave for the list: a page of a routine that is gone has nothing to reload. */
  remove(): void {
    if (this.page.kind !== "routine" || this.loaded.kind !== "routine") return;
    const routineId = this.page.routineId;
    const title = this.loaded.detail.row.title;
    void (async () => {
      this.isConfirmingRemoval = true;
      let isConfirmed: boolean;
      try {
        isConfirmed = await this.host.confirmRemove(title);
      } finally {
        this.isConfirmingRemoval = false;
      }
      if (!isConfirmed) return;
      const isRemoved = await this.act(async () => {
        await removeRoutine(routineId);
      }, "Couldn't remove it");
      if (!isRemoved) {
        await this.load();
        return;
      }
      this.dispose();
      this.host.navigate("/");
    })();
  }

  startWithText(text: string): void {
    this.host.startWithText(text);
  }
}

export interface AppAttrs {
  model: AppModel;
}

export const App: m.Component<AppAttrs> = {
  view({ attrs: { model } }) {
    const { loaded } = model;
    let body: m.Children;
    switch (loaded.kind) {
      case "loading":
        body = m("p", { class: "type-body p-6 text-faint", "data-loading": "" }, "Loading.");
        break;
      case "error":
        body = m(
          "p",
          { class: "type-body p-6 text-danger", "data-error": "" },
          `Couldn't read your routines: ${loaded.message}`,
        );
        break;
      case "list":
        body = m(ListPage, {
          listing: loaded.listing,
          onStartWithText: (text) => model.startWithText(text),
        });
        break;
      case "routine":
        body = m(RoutinePage, {
          detail: loaded.detail,
          isBusy: model.isBusy,
          onRunNow: () => model.runNow(),
          onToggleEnabled: () => model.toggleEnabled(),
          onChangeSchedule: () => model.openScheduleEditor(),
          onFix: () => model.fix(),
          onRemove: () => model.remove(),
        });
        break;
      case "run":
        body = m(RunPage, { detail: loaded.detail, routineTitle: loaded.routineTitle });
        break;
      case "log":
        body = m(LogPage, { run: loaded.run, log: loaded.log, routineTitle: loaded.routineTitle });
        break;
    }
    return [
      body,
      model.scheduleForm === null
        ? null
        : m(ScheduleDialog, {
            form: model.scheduleForm,
            isSaving: model.isBusy,
            onChange: (form) => {
              model.scheduleForm = form;
            },
            onSave: () => model.saveSchedule(),
            onCancel: () => model.closeScheduleEditor(),
          }),
      m(Toasts, {
        toasts: model.toasts.current(),
        placementClass: "fixed bottom-4",
        onDismiss: (id) => model.toasts.dismiss(id),
      }),
    ];
  },
};
