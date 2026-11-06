/**
 * The "Add app" dialog: install an app that is not in the store's own list, from a GitHub repository or from a
 * package on PyPI or npm.
 *
 * GitHub comes first, and works differently: the person pastes a link, and the dialog opens a chat with
 * ``/add-repo-to-store <link>`` (as installing a template does). That chat works out how to install the repository,
 * tries it out, and hands the store an install file, which the store then asks the person to confirm.
 *
 * It moves through four states. Entering details: the source, the package and an optional exact version, a display
 * name, the start command (shown as it will run, with sample values), and for Python the interpreter. Checking: the
 * store pins the version and estimates the disk without installing anything. Checked: what will be pinned, how many
 * packages it pulls in, the disk estimate and the fit, with memory "unknown until it runs" -- or the store's reason
 * for refusing. Confirming: nobody has reviewed this code, it runs with the same access as the user's other apps, and
 * it comes from this page on its registry. Confirming adds the app and queues its install like any other.
 *
 * The start command may be left empty: "Work it out for me" has the store install the package on the side, have AI
 * write the command, and test-launch it (retrying with what went wrong). While it works, the dialog shows what it is
 * doing and every command it tried. When a page answered, it shows a screenshot of it, says what the app is for, and
 * asks whether it looks right: yes fills the command in and checks it as if it were typed; no leaves it in the field
 * to edit. When the package is not an app, needs setting up first, or could not be started, it says so in plain words.
 *
 * Changing any detail after a check drops the check, so what is confirmed is always what was checked.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import { MODAL_LABEL_CLASS, MODAL_MESSAGE_CLASS, Modal } from "@imbue/workspace-ui/src/components/Modal";
import { SegmentedControl } from "@imbue/workspace-ui/src/components/segmentedControl";
import type {
  AddAppRequest,
  CheckAnswer,
  CheckResult,
  FindAnswer,
  FinderJob,
  PackageSource,
  SetupFile,
  WorkspaceRoom,
} from "../models/Store";
import { splitCommand } from "../models/Store";
import { formatMb } from "../room";
import { verdictBadge } from "./AppParts";

const DIALOG_WIDTH = 560;
// The window's height less the card's title, buttons and padding; the side padding keeps focus rings from clipping.
export const BODY_SCROLL_CLASS = "-mx-1 mb-2 max-h-[calc(100dvh-12rem)] overflow-y-auto px-1";
export const PYTHON_VERSIONS: readonly string[] = ["3.10", "3.11", "3.12", "3.13"];
export const SAMPLE_PORT = "8090";
export const UNREVIEWED_WARNING =
  "Nobody has reviewed this code. It will run with the same access as your other apps: it can read and change the " +
  "files in your workspace and reach the internet.";
const COMMAND_EXAMPLES: Record<PackageSource, string> = {
  pypi: "streamlit hello --server.port {port} --server.address 127.0.0.1 --server.headless true",
  npm: "http-server {data_dir} -p {port} -a 127.0.0.1",
};
export const COMMAND_PLACEHOLDER = "Leave empty to have it worked out for you";

export const FIND_POLL_MS = 1500;
export const FIND_ENDED_HEADINGS: Record<string, string> = {
  NOT_AN_APP: "This isn't an app with a window",
  NEEDS_SETUP: "This app needs setting up first",
  FAILED: "Couldn't get this app to start",
};

export type AddAppStage =
  | { kind: "editing"; refusal: string | null }
  | { kind: "finding"; job: FinderJob | null }
  | { kind: "found"; job: FinderJob }
  | { kind: "find-ended"; job: FinderJob }
  | { kind: "checking" }
  | { kind: "checked"; result: CheckResult }
  | { kind: "confirming"; result: CheckResult; refusal: string | null; isSubmitting: boolean };

/** How the dialog reaches the store's command finder. */
export interface FinderClient {
  start: (request: AddAppRequest) => Promise<FindAnswer>;
  /** Where a job stands; null when it could not be read this time. */
  poll: (jobId: string) => Promise<FinderJob | null>;
  cancel: (jobId: string) => Promise<void>;
  screenshotUrl: (jobId: string) => string;
  logUrl: (jobId: string) => string;
}

/** Where an app is added from: a GitHub repository (through a chat), or a package registry. */
export type AddOrigin = "github" | PackageSource;

export const GITHUB_COMMAND = "/add-repo-to-store";
// A link to a GitHub repository, as the chat's look-up reads it: the repository, or a branch, tag, release or commit.
const GITHUB_LINK_PATTERN =
  /^(?:https?:\/\/)?(?:www\.)?github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+?(?:\.git)?(?:\/(?:tree|commit|releases\/tag)\/[^\s?#]+?)?\/?(?:[?#]\S*)?$/;

/** Whether ``link`` is a link to a GitHub repository the chat can look up. */
export function isGithubLink(link: string): boolean {
  return GITHUB_LINK_PATTERN.test(link.trim());
}

export interface AddAppDialogAttrs {
  room: WorkspaceRoom;
  finder: FinderClient;
  /** Check a request without installing it. */
  onCheck: (request: AddAppRequest) => Promise<CheckAnswer>;
  /** Add and install; resolves to null when queued, else the reason it was refused. */
  onAdd: (request: AddAppRequest) => Promise<string | null>;
  /** Start a chat with this message (the shell's start-with-text), for a GitHub link. */
  onStartWithText: (text: string) => void;
  onClose: () => void;
}

/** The command as it will run, with sample values for the placeholders; null when its quotes do not close. */
export function commandPreview(command: string, slug: string): string[] | null {
  const words = splitCommand(command);
  if (words === null) return null;
  const dataDir = `data/.apps/app-store/${slug === "" ? "<name>" : slug}/data`;
  return words.map((word) => word.split("{port}").join(SAMPLE_PORT).split("{data_dir}").join(dataDir));
}

/** The internal name a display name gives, as the store derives it (for the preview only; the store decides). */
export function previewSlug(displayName: string): string {
  return displayName
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "")
    .replace(/^[0-9]+/, "")
    .slice(0, 31);
}

function fact(label: string, value: m.Children, hook?: string): m.Vnode[] {
  return [
    m("dt", { class: "type-helper text-faint" }, label),
    m("dd", { class: "m-0 type-body text-primary", ...(hook === undefined ? {} : { [`data-${hook}`]: "" }) }, value),
  ];
}

function quoted(words: readonly string[]): string {
  return words
    .map((word) => (/^[A-Za-z0-9_@%+=:,./{}-]+$/.test(word) ? word : `'${word.replace(/'/g, "'\\''")}'`))
    .join(" ");
}

export function AddAppDialog(): m.Component<AddAppDialogAttrs> {
  const request: AddAppRequest = {
    source: "pypi",
    package: "",
    version: "",
    display_name: "",
    command: "",
    python: "3.12",
    setup_files: [],
  };
  let stage: AddAppStage = { kind: "editing", refusal: null };
  let origin: AddOrigin = "github";
  let githubLink = "";
  let pollTimer: ReturnType<typeof setTimeout> | null = null;

  function isBusy(): boolean {
    return stage.kind === "checking" || stage.kind === "finding";
  }

  function change(update: Partial<AddAppRequest>): void {
    // Settings files were written for one package; another package starts without them.
    if (update.package !== undefined || update.source !== undefined) request.setup_files = [];
    Object.assign(request, update);
    if (!isBusy()) stage = { kind: "editing", refusal: null };
  }

  function stopPolling(): void {
    if (pollTimer !== null) clearTimeout(pollTimer);
    pollTimer = null;
  }

  async function find(attrs: AddAppDialogAttrs): Promise<void> {
    stage = { kind: "finding", job: null };
    const answer = await attrs.finder.start({ ...request });
    if (answer.kind === "refused") {
      stage = { kind: "editing", refusal: answer.reason };
    } else {
      followJob(attrs, answer.job);
    }
    m.redraw();
  }

  function followJob(attrs: AddAppDialogAttrs, job: FinderJob): void {
    if (stage.kind !== "finding") return;
    switch (job.status) {
      case "WORKING":
        stage = { kind: "finding", job };
        stopPolling();
        pollTimer = setTimeout(() => {
          void attrs.finder.poll(job.job_id).then((next) => {
            followJob(attrs, next ?? job);
            m.redraw();
          });
        }, FIND_POLL_MS);
        return;
      case "FOUND":
        stage = { kind: "found", job };
        return;
      case "CANCELLED":
        stage = { kind: "editing", refusal: null };
        return;
      default:
        stage = { kind: "find-ended", job };
    }
  }

  function cancelFind(attrs: AddAppDialogAttrs): void {
    if (stage.kind === "finding" && stage.job !== null) void attrs.finder.cancel(stage.job.job_id);
    stopPolling();
    stage = { kind: "editing", refusal: null };
  }

  function useFound(attrs: AddAppDialogAttrs, job: FinderJob): void {
    request.command = job.command;
    request.setup_files = job.setup_files;
    if (job.version !== "" && request.version.trim() === "") request.version = job.version;
    void check(attrs);
  }

  async function check(attrs: AddAppDialogAttrs): Promise<void> {
    stage = { kind: "checking" };
    const answer = await attrs.onCheck({ ...request });
    stage =
      answer.kind === "checked"
        ? { kind: "checked", result: answer.result }
        : { kind: "editing", refusal: answer.reason };
    m.redraw();
  }

  async function add(attrs: AddAppDialogAttrs, result: CheckResult): Promise<void> {
    stage = { kind: "confirming", result, refusal: null, isSubmitting: true };
    const refusal = await attrs.onAdd({ ...request, version: result.version });
    if (refusal === null) {
      attrs.onClose();
    } else {
      stage = { kind: "confirming", result, refusal, isSubmitting: false };
    }
    m.redraw();
  }

  function textField(
    label: string,
    hook: string,
    value: string,
    placeholder: string,
    onInput: (value: string) => void,
  ): m.Vnode {
    return m("label", { class: "mb-3 block" }, [
      m("span", { class: MODAL_LABEL_CLASS }, label),
      m("input", {
        type: "text",
        [`data-field`]: hook,
        value,
        placeholder,
        spellcheck: false,
        autocomplete: "off",
        disabled: isBusy(),
        class: inputClass({ extra: hook === "command" ? "font-mono" : "" }),
        oninput: (event: InputEvent) => onInput((event.target as HTMLInputElement).value),
      }),
    ]);
  }

  function originPicker(): m.Vnode {
    return m("div", { class: "mb-3 flex items-center gap-3" }, [
      m("span", { class: "type-helper text-secondary" }, "From"),
      m(SegmentedControl<AddOrigin>, {
        label: "Where the app comes from",
        segments: [
          { value: "github", label: "GitHub", attrs: { "data-source": "github" } },
          { value: "pypi", label: "Python (PyPI)", attrs: { "data-source": "pypi" } },
          { value: "npm", label: "Node (npm)", attrs: { "data-source": "npm" } },
        ],
        value: origin,
        onSelect: (picked: AddOrigin) => {
          if (picked !== "github") {
            origin = picked;
            change({ source: picked });
          } else if (!isBusy()) {
            // The link form replaces whatever a package's check or search left on screen.
            origin = picked;
            stage = { kind: "editing", refusal: null };
          }
        },
      }),
    ]);
  }

  function githubForm(): m.Children {
    const link = githubLink.trim();
    return [
      originPicker(),
      m("label", { class: "mb-3 block" }, [
        m("span", { class: MODAL_LABEL_CLASS }, "Link to the project on GitHub"),
        m("input", {
          type: "url",
          "data-field": "github-link",
          value: githubLink,
          placeholder: "https://github.com/owner/project",
          spellcheck: false,
          autocomplete: "off",
          class: inputClass({}),
          oninput: (event: InputEvent) => (githubLink = (event.target as HTMLInputElement).value),
        }),
      ]),
      link !== "" && !isGithubLink(link)
        ? m(
            "p",
            { class: "m-0 -mt-2 mb-3 type-helper text-danger", "data-link-problem": "" },
            "That isn't a link to a project on GitHub, like https://github.com/owner/project.",
          )
        : null,
      m(
        "p",
        { class: "m-0 mb-3 type-helper text-secondary" },
        "A chat opens and works out how to install it: it picks the project's latest stable version, tries the app " +
          "out safely, and checks how much disk and memory it needs. Then this store asks you to confirm before " +
          "anything is installed. Public projects only, for now.",
      ),
    ];
  }

  function form(): m.Children {
    const displayName = request.display_name.trim() || request.package.trim().split("/").pop() || "";
    const preview = commandPreview(request.command, previewSlug(displayName));
    return [
      originPicker(),
      m("div", { class: "grid grid-cols-[2fr_1fr] gap-3" }, [
        textField(
          "Package name",
          "package",
          request.package,
          request.source === "pypi" ? "streamlit" : "http-server",
          (value) => change({ package: value }),
        ),
        textField("Exact version", "version", request.version, "latest", (value) => change({ version: value })),
      ]),
      textField(
        "Display name",
        "display-name",
        request.display_name,
        request.package.trim() || "Shown on your desktop",
        (value) => change({ display_name: value }),
      ),
      textField("Start command", "command", request.command, COMMAND_PLACEHOLDER, (value) =>
        change({ command: value }),
      ),
      m(
        "p",
        { class: "m-0 -mt-2 mb-3 type-helper text-secondary" },
        `Not sure? Leave it empty and the store will work it out and test it for you. Otherwise: the program's name, then its arguments, like ${COMMAND_EXAMPLES[request.source]}. Use {port} for the port it must listen on (required) and {data_dir} for its own folder. It must listen on 127.0.0.1. No shell runs it: quotes group words, nothing else is special.`,
      ),
      request.command.trim() === ""
        ? null
        : m("div", { class: "mb-3", "data-command-preview": "" }, [
            m("span", { class: MODAL_LABEL_CLASS }, "Will run (the program from the app's own folder)"),
            preview === null
              ? m("p", { class: "m-0 type-helper text-danger" }, "A quote isn't closed.")
              : m(
                  "code",
                  { class: "block rounded-md bg-surface-secondary px-2 py-1.5 font-mono type-helper break-all" },
                  quoted(preview),
                ),
          ]),
      request.setup_files.length === 0
        ? null
        : m("div", { class: "mb-3", "data-form-setup-files": "" }, [
            setupFileList(request.setup_files),
            m(
              "button",
              {
                type: "button",
                class: "type-helper text-accent hover:underline",
                disabled: isBusy(),
                "data-remove-setup-files": "",
                onclick: () => change({ setup_files: [] }),
              },
              "Start without these files",
            ),
          ]),
      request.source === "pypi"
        ? m("label", { class: "mb-3 flex items-center gap-3" }, [
            m("span", { class: "type-helper text-secondary" }, "Python version"),
            m(
              "select",
              {
                "data-field": "python",
                class: inputClass({ extra: "w-auto" }),
                disabled: isBusy(),
                value: request.python,
                onchange: (event: Event) => change({ python: (event.target as HTMLSelectElement).value }),
              },
              PYTHON_VERSIONS.map((version) => m("option", { value: version }, version)),
            ),
          ])
        : null,
    ];
  }

  function setupFileList(files: readonly SetupFile[]): m.Children {
    if (files.length === 0) return null;
    return m("div", { class: "mt-2", "data-setup-files": "" }, [
      m("span", { class: MODAL_LABEL_CLASS }, "With these settings files in its folder"),
      files.map((file) =>
        m("details", { class: "mb-1", "data-setup-file": file.path }, [
          m("summary", { class: "cursor-pointer font-mono type-helper text-primary" }, file.path),
          m(
            "pre",
            {
              class:
                "m-0 mt-1 max-h-48 overflow-auto rounded-md bg-surface-secondary px-2 py-1.5 font-mono type-helper whitespace-pre-wrap",
            },
            file.content,
          ),
        ]),
      ),
    ]);
  }

  function attemptList(job: FinderJob): m.Children {
    if (job.attempts.length === 0) return null;
    return m("div", { class: "mt-3", "data-attempts": "" }, [
      m("span", { class: MODAL_LABEL_CLASS }, "What it tried"),
      m(
        "ol",
        { class: "m-0 pl-5 type-helper text-secondary" },
        job.attempts.map((attempt) =>
          m("li", { class: "mb-1" }, [
            attempt.outcome,
            m("code", { class: "block font-mono text-faint break-all" }, attempt.command),
          ]),
        ),
      ),
    ]);
  }

  function logLink(attrs: AddAppDialogAttrs, job: FinderJob): m.Vnode {
    return m(
      "a",
      {
        class: "type-helper text-faint hover:underline",
        href: attrs.finder.logUrl(job.job_id),
        target: "_blank",
        rel: "noopener noreferrer",
        "data-finder-log": "",
      },
      "Full log",
    );
  }

  function findingPanel(job: FinderJob | null): m.Vnode {
    return m("div", { class: "mb-4 rounded-lg border border-default p-3", "data-finding": "" }, [
      m("p", { class: "m-0 flex items-center gap-2 type-body text-primary", "data-progress": "" }, [
        m("span", { class: "spinner spinner--sm spinner--current" }),
        job === null || job.progress === "" ? "Starting…" : `${job.progress}…`,
      ]),
      m(
        "p",
        { class: "m-0 mt-2 type-helper text-secondary" },
        "It downloads the app to a scratch folder, has AI write the start command, and test-launches it. Nothing is installed until you say so.",
      ),
      job === null ? null : attemptList(job),
    ]);
  }

  function foundPanel(attrs: AddAppDialogAttrs, job: FinderJob): m.Vnode {
    return m("div", { class: "mb-4", "data-found": "" }, [
      job.has_screenshot
        ? m("img", {
            src: attrs.finder.screenshotUrl(job.job_id),
            alt: `${job.package} as it started`,
            class: "mb-3 block w-full rounded-lg border border-default",
            "data-screenshot": "",
          })
        : m(
            "p",
            { class: "m-0 mb-3 type-helper text-secondary", "data-no-screenshot": "" },
            "It started and showed a page, but a picture of it couldn't be taken.",
          ),
      job.explanation === "" ? null : m("p", { class: MODAL_MESSAGE_CLASS, "data-explanation": "" }, job.explanation),
      m("p", { class: "m-0 mb-2 type-body font-semibold text-primary" }, "Does this look right?"),
      m("span", { class: MODAL_LABEL_CLASS }, "It starts with"),
      m(
        "code",
        {
          class: "block rounded-md bg-surface-secondary px-2 py-1.5 font-mono type-helper break-all",
          "data-found-command": "",
        },
        job.command,
      ),
      setupFileList(job.setup_files),
      m("div", { class: "mt-2" }, logLink(attrs, job)),
    ]);
  }

  function endedPanel(attrs: AddAppDialogAttrs, job: FinderJob): m.Vnode {
    return m("div", { class: "mb-4 rounded-lg border border-default p-3", "data-find-ended": job.status }, [
      m("p", { class: "m-0 mb-2 type-body font-semibold text-primary" }, FIND_ENDED_HEADINGS[job.status] ?? ""),
      m("p", { class: MODAL_MESSAGE_CLASS, "data-explanation": "" }, job.explanation),
      attemptList(job),
      m("p", { class: "m-0 mt-3 type-helper text-faint" }, ["Nothing was left installed. ", logLink(attrs, job)]),
    ]);
  }

  function checkedPanel(result: CheckResult, room: WorkspaceRoom): m.Vnode {
    return m("div", { class: "mb-4 rounded-lg border border-default p-3", "data-check-result": "" }, [
      m("dl", { class: "m-0 grid grid-cols-[8rem_1fr] gap-x-4 gap-y-1.5" }, [
        ...fact(
          "Will install",
          [`${result.package} `, m("span", { class: "font-semibold" }, result.version)],
          "pinned",
        ),
        ...fact(
          "Packages",
          result.package_count === null ? "not resolved" : `${result.package_count} in all`,
          "package-count",
        ),
        ...fact(
          "Disk",
          result.disk_mb === null
            ? "unknown"
            : [
                `about ${formatMb(result.disk_mb)} `,
                m("span", { class: "type-helper text-faint" }, `(${formatMb(room.disk_free_mb)} free)`),
              ],
          "disk",
        ),
        ...fact("Memory", "unknown until it runs", "memory"),
        ...fact("Room", verdictBadge(result.verdict)),
      ]),
      m("p", { class: "m-0 mt-2 type-helper text-secondary", "data-disk-basis": "" }, result.disk_basis),
      result.blocked_reason === ""
        ? null
        : m("p", { class: "m-0 mt-2 type-helper text-danger", "data-refusal": "" }, result.blocked_reason),
    ]);
  }

  function confirmation(result: CheckResult): m.Vnode {
    return m("div", { class: "mb-4", "data-confirmation": "" }, [
      m(
        "p",
        { class: "m-0 mb-3 rounded-lg bg-warning-surface px-3 py-2 type-body text-primary", "data-warning": "" },
        UNREVIEWED_WARNING,
      ),
      m("p", { class: MODAL_MESSAGE_CLASS }, [
        `It comes from ${result.registry}: `,
        m(
          "a",
          {
            class: "text-accent hover:underline",
            href: result.registry_url,
            target: "_blank",
            rel: "noopener noreferrer",
          },
          `${result.package} ${result.version}`,
        ),
        result.license === "" ? "." : `, under ${result.license}.`,
        ` It will run as: `,
        m("code", { class: "font-mono type-helper" }, quoted(result.command_preview)),
      ]),
      setupFileList(result.setup_files),
    ]);
  }

  let currentAttrs: AddAppDialogAttrs | null = null;

  return {
    onremove() {
      if (currentAttrs !== null && stage.kind === "finding") cancelFind(currentAttrs);
      stopPolling();
    },
    view(vnode) {
      const attrs = vnode.attrs;
      currentAttrs = attrs;
      let body: m.Children;
      let actions: m.Children;
      const isGithub = origin === "github";
      if (isGithub) {
        const link = githubLink.trim();
        body = githubForm();
        actions = [
          m(Button, { onclick: attrs.onClose }, "Cancel"),
          m(
            Button,
            {
              variant: "primary",
              extra: "app-store-github-chat",
              disabled: !isGithubLink(link),
              onclick: () => {
                attrs.onStartWithText(`${GITHUB_COMMAND} ${link}`);
                attrs.onClose();
              },
            },
            "Work it out in a chat",
          ),
        ];
      } else
        switch (stage.kind) {
          case "editing":
          case "checking": {
            const isFinding = stage.kind === "editing" && request.command.trim() === "";
            body = [
              form(),
              stage.kind === "editing" && stage.refusal !== null
                ? m("p", { class: "m-0 mb-4 type-helper text-danger", "data-refusal": "" }, stage.refusal)
                : null,
            ];
            actions = [
              m(Button, { onclick: attrs.onClose }, "Cancel"),
              isFinding
                ? m(
                    Button,
                    {
                      variant: "primary",
                      extra: "app-store-find",
                      disabled: request.package.trim() === "",
                      onclick: () => void find(attrs),
                    },
                    "Work it out for me",
                  )
                : m(
                    Button,
                    {
                      variant: "primary",
                      extra: "app-store-check",
                      disabled: stage.kind === "checking" || request.package.trim() === "",
                      onclick: () => void check(attrs),
                    },
                    stage.kind === "checking"
                      ? [m("span", { class: "spinner spinner--sm spinner--current" }), "Checking…"]
                      : "Check",
                  ),
            ];
            break;
          }
          case "finding":
            body = [form(), findingPanel(stage.job)];
            actions = [m(Button, { extra: "app-store-stop-find", onclick: () => cancelFind(attrs) }, "Stop")];
            break;
          case "found": {
            const job = stage.job;
            body = foundPanel(attrs, job);
            actions = [
              m(
                Button,
                {
                  extra: "app-store-type-myself",
                  onclick: () => {
                    request.command = job.command;
                    request.setup_files = job.setup_files;
                    stage = { kind: "editing", refusal: null };
                  },
                },
                "No, I'll change it",
              ),
              m(
                Button,
                { variant: "primary", extra: "app-store-use-found", onclick: () => useFound(attrs, job) },
                "Yes, use this",
              ),
            ];
            break;
          }
          case "find-ended":
            body = [form(), endedPanel(attrs, stage.job)];
            actions = [
              m(Button, { onclick: attrs.onClose }, "Close"),
              m(
                Button,
                { extra: "app-store-back-to-form", onclick: () => (stage = { kind: "editing", refusal: null }) },
                stage.job.status === "FAILED" ? "Type the command myself" : "Try another package",
              ),
            ];
            break;
          case "checked":
            body = [form(), checkedPanel(stage.result, attrs.room)];
            actions = [
              m(Button, { onclick: attrs.onClose }, "Cancel"),
              m(
                Button,
                {
                  variant: "primary",
                  extra: "app-store-continue",
                  disabled: stage.result.verdict === "WONT_FIT",
                  onclick: () => {
                    if (stage.kind === "checked")
                      stage = { kind: "confirming", result: stage.result, refusal: null, isSubmitting: false };
                  },
                },
                "Continue",
              ),
            ];
            break;
          case "confirming": {
            const confirming = stage;
            body = [
              checkedPanel(confirming.result, attrs.room),
              confirmation(confirming.result),
              confirming.refusal === null
                ? null
                : m("p", { class: "m-0 mb-4 type-helper text-danger", "data-refusal": "" }, confirming.refusal),
            ];
            actions = [
              m(Button, { onclick: () => (stage = { kind: "checked", result: confirming.result }) }, "Back"),
              m(
                Button,
                {
                  variant: "primary",
                  extra: "app-store-confirm-add",
                  disabled: confirming.isSubmitting,
                  onclick: () => void add(attrs, confirming.result),
                },
                `Install ${confirming.result.title}`,
              ),
            ];
            break;
          }
        }
      return m(
        Modal,
        {
          title: "Add an app",
          width: DIALOG_WIDTH,
          onDismiss: attrs.onClose,
          onEscape: attrs.onClose,
          card: { role: "dialog", "aria-modal": "true", "data-dialog": "add-app", "data-stage": stage.kind },
          actions,
        },
        // The body scrolls on its own, so the buttons below it stay on screen however tall it grows.
        m("div", { class: BODY_SCROLL_CLASS, "data-dialog-body": "" }, [
          m(
            "p",
            { class: MODAL_MESSAGE_CLASS },
            isGithub
              ? "Add an app from a project on GitHub that isn't in the store."
              : "Install an app from a package on PyPI or npm that isn't in the store. Check it first to see what it will install and cost.",
          ),
          body,
        ]),
      );
    },
  };
}
