"""The shell's recovery page: what the shell serves in place of the desktop when its frontend is not built, or when
the read of the desktops and apps the frontend boots from failed (desktop contracts.md section 5.1)."""

import html
import json
import re
import shlex
from typing import Final
from typing import assert_never

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.documents import FRONTEND_BUILT_HEADER
from imbue.system_interface.shell.recovery_launches import RecoveryReason

# The route the page runs its launches through: the shell posts them to the apps over loopback, since the page cannot
# post across origins itself.
RECOVERY_LAUNCH_PATH: Final[str] = "/api/recovery/launch"

# Where the frontend sends the page when its startup read of the desktops and apps got an error answer.
DESKTOPS_RECOVERY_PATH: Final[str] = "/recovery/desktops"

# The read the desktops page polls, the one the frontend boots from.
_INVENTORY_PATH: Final[str] = "/api/inventory"

# How often the page asks whether what it is about is fixed. Also the interval of the scriptless fallback below, so
# both routes behave the same.
RECOVERY_POLL_SECONDS: Final[int] = 10

# The pieces of workspace_ui's ``agentFixTask`` and ``assistRequest`` (``app_contract.ts``), which this page cannot
# import: it is served exactly when the bundle may be missing. A ``frontend`` test pins them to that source.
ASSIST_COMMAND: Final[str] = "/assist"
AGENT_FIX_TASK_LEAD: Final[str] = "Imbue Studio showed the user this failure: "
AGENT_FIX_TASK_DETAILS_LABEL: Final[str] = "Details:"
AGENT_FIX_TASK_CLOSING: Final[str] = (
    "Find out why it failed and fix it, then tell the user what was wrong and what you changed."
)
_SHORTEST_FENCE_LENGTH: Final[int] = 3

INTERFACE_NOT_BUILT_HEADING: Final[str] = "This workspace's interface needs to be rebuilt"
DESKTOPS_NOT_LOADED_SENTENCE: Final[str] = "This workspace's desktops couldn't be loaded."
DESKTOPS_OUT_OF_SPACE_SENTENCE: Final[str] = "This workspace is out of space, so its desktops couldn't be loaded."


@pure
def assist_request(request: str) -> str:
    """``request`` behind the command that runs the workspace's assist flow, which a chat runs only at the start of its
    first message (``assistRequest``)."""
    return f"{ASSIST_COMMAND} {request}"


@pure
def agent_fix_task(what_failed: str, details: str | None) -> str:
    """The task every "Ask an agent to fix this" gives the agent, as ``agentFixTask`` words it: what failed, the details
    fenced as they are, and what to do, said by Imbue Studio about the user."""
    stem = what_failed.strip()
    parts = [f"{AGENT_FIX_TASK_LEAD}{stem if re.search(r'[.!?]$', stem) else f'{stem}.'}"]
    if details is not None and details.strip() != "":
        longest_run = max((len(run) for run in re.findall(r"`+", details)), default=0)
        fence = "`" * max(_SHORTEST_FENCE_LENGTH, longest_run + 1)
        parts.append(f"{AGENT_FIX_TASK_DETAILS_LABEL}\n{fence}\n{details}\n{fence}")
    parts.append(AGENT_FIX_TASK_CLOSING)
    return "\n\n".join(parts)


# The ``mngr`` invocation the page offers for standing up an agent to repair the workspace, before its message. It
# mirrors what the chat app's ``_build_chat_create_command`` runs for a chat -- ``--template chat`` for the shared work
# directory, the output style, and running in the workspace tree rather than a worktree of it, plus the
# ``user_created`` label that puts the agent in the dynamic chat memory band. Where it differs from that builder:
#
# No ``--type``, which is the one thing that builder cannot leave out: it is serving a menu entry, so the harness is a
# choice the user already made. This page has no such choice to carry, so leaving the flag off lets ``mngr`` resolve
# the harness from ``[commands.create] type`` -- whatever this workspace is configured to open chats as, rather than
# whatever it was when this string was written. ``--template chat`` supplies the rest either way: it is harness-
# agnostic (``output_style`` is honored by the claude, codex and pi plugins alike), so it does not pick one and must
# not be relied on to.
#
# No ``--transfer none`` either, for a different reason: the ``chat`` template already sets it, and the builder spells
# it out only because it is assembling an argv rather than a line for a reader. It is not optional the way the harness
# is -- an agent in a worktree would repair a copy of the workspace instead of the workspace -- so
# ``test_chat_system.py`` reads the template and fails if that setting ever leaves it, rather than trusting this
# comment.
#
# ``--connect`` instead of its ``--no-connect``, which exists to keep a headless caller from attaching. Someone typing
# this wants the opposite, and connecting is what turns the create into a conversation. Not merely explicit for the
# reader's sake: the workspace's own ``[commands.create] connect = false`` is the default this overrides.
#
# ``--message`` so the agent opens already knowing what the reader is looking at: the message the page's "Ask an
# agent" button sends, so the button and the line ask for the same thing.
#
# No name, so mngr mints one. A reload, a second tab, or a first attempt that did not finish therefore starts a fresh
# conversation rather than rejoining an earlier one.
#
# Kept in sync with that builder by ``test_chat_system.py``, which also validates it against the live CLI. The server
# never runs it: the page's button sends the same message through the workspace's primary text action instead, so an
# agent is asked only if the reader presses it.
_REPAIR_MNGR_CREATE: Final[str] = "mngr create --connect --template chat --label user_created=true --message "

# What a double-quoted word still expands, each kept literal by a backslash.
_DOUBLE_QUOTE_SPECIAL_PATTERN: Final[re.Pattern[str]] = re.compile(r'([\\"$`])')


@pure
def _double_quoted_for_a_reader(text: str) -> str:
    """``text`` as one shell word in ``"``, rather than as ``shlex.quote`` would (``'workspace'"'"'s ...``): the readable
    form, for the one line on the page a reader has to be able to read in full. What a shell would expand inside the
    quotes is escaped, and a ``!`` (which an interactive bash or zsh expands from its history even there) is closed
    out into ``'!'``."""
    escaped = _DOUBLE_QUOTE_SPECIAL_PATTERN.sub(r"\\\1", text)
    return '"' + escaped.replace("!", "\"'!'\"") + '"'


@pure
def repair_mngr_command(message: str) -> str:
    """The ``mngr create`` line that starts a chat whose first message is ``message``, written as a reader sees it."""
    return _REPAIR_MNGR_CREATE + _double_quoted_for_a_reader(message)


@pure
def repair_command(message: str) -> str:
    """The line the page offers: ``repair_mngr_command`` with ``TMUX`` dropped for it alone.

    ``mngr connect`` refuses to attach from inside tmux unless ``is_nested_tmux_allowed`` is set (see
    ``mngr.api.connect``, which gates purely on ``$TMUX``), and it is the connect half of the create that would hit it.
    The workspace's own terminal tabs are tmux sessions, so a reader who runs this in one would get a created agent and
    an error instead of a conversation. Dropping ``TMUX`` for this one command is exactly what mngr does for itself once
    the check passes, and scoping it with ``env -u`` leaves the reader's own shell alone.
    """
    return "env -u TMUX " + repair_mngr_command(message)


# What the repair agent is told while the interface is not built: the page's own heading, the one detail a reader on
# this page can be certain of and the one the agent can act on without being told anything else.
INTERFACE_NOT_BUILT_REPAIR_MESSAGE: Final[str] = assist_request(
    f"{AGENT_FIX_TASK_LEAD}{INTERFACE_NOT_BUILT_HEADING}. {AGENT_FIX_TASK_CLOSING}"
)
INTERFACE_NOT_BUILT_REPAIR_MNGR_COMMAND: Final[str] = repair_mngr_command(INTERFACE_NOT_BUILT_REPAIR_MESSAGE)
INTERFACE_NOT_BUILT_REPAIR_ARGV: Final[tuple[str, ...]] = tuple(shlex.split(INTERFACE_NOT_BUILT_REPAIR_MNGR_COMMAND))


class DesktopsLoadFailure(FrozenModel):
    """Why the read of the desktops and apps failed, as the shell itself raised it."""

    details: str = Field(description="The shell's own reason, for See details and the agent")
    is_out_of_space: bool = Field(description="Whether the read failed because the workspace's disk is full")


@pure
def desktops_failure_sentence(failure: DesktopsLoadFailure) -> str:
    return DESKTOPS_OUT_OF_SPACE_SENTENCE if failure.is_out_of_space else DESKTOPS_NOT_LOADED_SENTENCE


@pure
def desktops_repair_message(failure: DesktopsLoadFailure) -> str:
    """What the repair agent is told when the desktops couldn't be loaded: the page's sentence and the failure's
    details, as every ask of the desktop's is worded."""
    return assist_request(agent_fix_task(desktops_failure_sentence(failure), failure.details))


class RecoveryPageContent(FrozenModel):
    """What the recovery page says for one reason, and what its script does."""

    reason: RecoveryReason = Field(description="What the page is about, which its launches name")
    title: str = Field(description="The document's title")
    heading: str = Field(description="The page's one sentence of what is wrong")
    explanation: str = Field(description="The paragraph under the heading")
    ask_lead: str | None = Field(description="The line before the agent button, if any")
    ask_label: str = Field(description="The agent button's label")
    is_reload_offered: bool = Field(description="Whether a Reload button goes back to the desktop")
    failure_details: str | None = Field(description="The failure's own details, first under See details, if any")
    repair_command: str = Field(description="The shell line that starts a repair agent by hand")


INTERFACE_NOT_BUILT_PAGE: Final[RecoveryPageContent] = RecoveryPageContent(
    reason=RecoveryReason.INTERFACE_NOT_BUILT,
    title="Interface not built",
    heading=INTERFACE_NOT_BUILT_HEADING,
    explanation=(
        "The compiled interface is missing, so there is nothing to show yet. Your work and your agents are untouched "
        "-- only the interface itself is gone, and this page returns to the interface on its own once it is back."
    ),
    ask_lead="If it isn't back in a few minutes, an agent can fix it.",
    ask_label="Ask an agent to fix this",
    is_reload_offered=False,
    failure_details=None,
    repair_command=repair_command(INTERFACE_NOT_BUILT_REPAIR_MESSAGE),
)


@pure
def desktops_not_loaded_page(failure: DesktopsLoadFailure) -> RecoveryPageContent:
    """The page for a read of the desktops that failed: its sentence, its details, and an agent asked about it (to free
    up space, on a full disk)."""
    return RecoveryPageContent(
        reason=RecoveryReason.DESKTOPS_NOT_LOADED,
        title="Desktops couldn't be loaded",
        heading=desktops_failure_sentence(failure),
        explanation="This page goes back to your desktop on its own once they load.",
        ask_lead=None,
        ask_label="Ask an agent to free up space" if failure.is_out_of_space else "Ask an agent to fix this",
        is_reload_offered=True,
        failure_details=failure.details,
        repair_command=repair_command(desktops_repair_message(failure)),
    )


# The page asks whether the bundle is back rather than reloading blind. The reply carries the same header the reveal
# flow's own health check reads, so a rebuild by ANY route -- a rollback, a build run in the terminal -- brings the
# interface back with no further action. Asking (rather than refreshing) is what lets the terminal stay alive between
# checks. The backend restarting is the expected way for it to fail, and also a moment when the bundle may be
# arriving, so a failure says nothing.
_INTERFACE_NOT_BUILT_CHECK: Final[str] = """function checkRecovered() {
    fetch(window.location.pathname, { method: "HEAD", cache: "no-store" })
      .then(function (response) {
        if (response.headers.get("__BUILT_HEADER__") === "true") {
          window.location.reload();
        }
      })
      .catch(function () {});
  }"""

# The page reads the desktops and apps as the frontend does, and goes back to the desktop once a read answers.
_DESKTOPS_NOT_LOADED_CHECK: Final[str] = """function checkRecovered() {
    fetch(__INVENTORY_URL__, { cache: "no-store" })
      .then(function (response) {
        if (response.ok) returnToDesktop();
      })
      .catch(function () {});
  }"""

# Served in place of the desktop. The way out is an agent, and a terminal behind "See details", not a "rebuild" or
# "retry" button. A button has to be right about what went wrong; the states that strand a workspace here are
# dominated by ones where a repair dispatched from the server would fail too (no registry, no memory, a lockfile that
# does not resolve, a full disk), and it would fail with nowhere to say so, on a page with no application to render
# the failure. It would also inherit the server's memory band, which puts it ahead of the user's chats and agents in a
# shed. An agent and a shell are the general case of every repair rather than one of them, and both run in apps that
# are already running and already *more* protected than this server: the page asks them, through the shell's
# launches, rather than repairing anything.
#
# Nothing here is part of the compiled bundle -- this is a string in the backend -- so the script below ships whether
# or not the frontend has ever been built. That is what lets the page be more than static text in exactly the state
# where the frontend is missing.
_RECOVERY_PAGE_TEMPLATE: Final[str] = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; display: flex;
         align-items: center; justify-content: center; min-height: 100vh;
         background: #14161a; color: #e6e8eb; }
  main { max-width: 48rem; padding: 2rem; width: 100%; box-sizing: border-box; }
  h1 { font-size: 1.25rem; font-weight: 600; margin: 0 0 0.75rem; }
  p { line-height: 1.5; margin: 0 0 1rem; color: #b6bcc4; max-width: 34rem; }
  code { background: #1d2026; border-radius: 4px; padding: 0.1rem 0.3rem;
         font-size: 0.9em; }
  /* The command wraps rather than scrolling: a horizontal scrollbar hides
     most of it behind a control nobody looks for, and this is the one line on
     the page a reader has to be able to read in full. */
  #repair { position: relative; }
  pre { background: #1d2026; border-radius: 6px; padding: 0.75rem 1rem;
        margin: 0 0 1rem; font-size: 0.85em; color: #e6e8eb;
        white-space: pre-wrap; overflow-wrap: anywhere; }
  /* Room for the copy button, so the last line never runs underneath it. */
  #repair-command { padding-right: 5.5rem; }
  #copy-repair { position: absolute; top: 0.5rem; right: 0.5rem;
                 font: inherit; font-size: 0.8em; padding: 0.25rem 0.6rem;
                 color: #e6e8eb; background: #2a2f37; border: 1px solid #3a414b;
                 border-radius: 4px; cursor: pointer; }
  #copy-repair:hover { background: #333a44; }
  #copy-repair[hidden] { display: none; }
  [hidden] { display: none; }
  #terminal { width: 100%; height: 24rem; border: 1px solid #2a2f37;
              border-radius: 6px; background: #000; }
  #agent-chat { width: 100%; height: 24rem; border: 1px solid #2a2f37;
                border-radius: 6px; background: #fff; }
  .action { font: inherit; padding: 0.4rem 1rem; color: #e6e8eb; background: #2a2f37;
            border: 1px solid #3a414b; border-radius: 6px; cursor: pointer; }
  .action:hover { background: #333a44; }
  .action:disabled { cursor: default; opacity: 0.6; }
  #page-actions { display: flex; flex-wrap: wrap; gap: 0.5rem; }
  details { margin-top: 1.5rem; color: #8a929c; }
  summary { cursor: pointer; margin-bottom: 0.75rem; }
</style>
<!-- The scriptless fallback for the poll at the end of the body. Whole-page
     reloads and a live terminal cannot coexist -- a refresh every ten seconds
     would destroy the session the reader is typing into -- so this runs only
     where there is no script to poll with, and therefore no terminal either. -->
<noscript><meta http-equiv="refresh" content="__POLL_SECONDS__"></noscript>
</head>
<body>
<main>
  <h1>__HEADING__</h1>
  <p>__EXPLANATION__</p>
  <div id="ask-agent">
    __ASK_LEAD__
    <!-- The agent button is shown only when the workspace has an app to ask an agent through. -->
    <p id="page-actions"><button id="ask-agent-button" class="action" type="button" hidden>__ASK_LABEL__</button>__RELOAD_BUTTON__</p>
    <p id="ask-agent-failure-message" hidden></p>
    <!-- Once an ask failed, the way out the reader is left with: the command, and the terminal to run it in. -->
    <div id="ask-agent-fallback" hidden></div>
    <p id="agent-started" hidden></p>
    <div id="agent-chat-slot" hidden>
      <iframe id="agent-chat" title="The chat the agent was asked in"></iframe>
    </div>
  </div>
  <details id="repair-details">
    <summary>See details</summary>
    __FAILURE_DETAILS__
    <div id="ask-agent-failure-reason" hidden>
      <p>Why the agent couldn't be asked:</p>
      <p id="ask-agent-failure-sentence"></p>
      <pre id="ask-agent-failure-details"></pre>
    </div>
    <p id="repair-intro">Run in a terminal, this starts a new chat with an agent and tells it what you are looking
       at, for when an agent can't be asked from this page:</p>
    <div id="repair">
      <pre id="repair-command">__REPAIR_COMMAND__</pre>
      <!-- Hidden until the script confirms it can actually copy, so the page
           never shows a button that does nothing. -->
      <button id="copy-repair" type="button" hidden>Copy</button>
    </div>
    <p id="no-terminal">The workspace's terminal isn't running yet, so there is
       nowhere to run this here.</p>
    <details id="terminal-details" hidden>
      <summary>Open this workspace's terminal</summary>
      <!-- The lead-in belongs to the terminal, not to the command: the command
           stands on its own wherever the reader finds a shell, so it keeps its own
           introduction above and is never left orphaned when there is no terminal
           to offer. -->
      <p id="terminal-intro" hidden>You can run that -- or do the repair yourself --
         in this workspace's terminal, below.</p>
      <p id="terminal-failure" hidden></p>
      <div id="terminal-slot" hidden>
        <iframe id="terminal" title="Workspace terminal"></iframe>
      </div>
    </details>
  </details>
</main>
<script>
(function () {
  // The terminal's hostname label, minted per workspace, or "" when there is
  // no terminal registered to offer.
  var terminalLabel = __TERMINAL_LABEL__;
  // Whether an app takes the text an agent is asked with (Chat, on a stock workspace).
  var isAgentOffered = __IS_AGENT_OFFERED__;
  var launchUrl = __LAUNCH_URL__;
  var reason = __REASON__;
  // The desktop, with the query the shell's page was opened with (a pulled-out window's own page keeps its own).
  var desktopUrl = __DESKTOP_URL__;

  function returnToDesktop() {
    window.location.replace(desktopUrl + window.location.search);
  }

  // Mirrors deriveAppOrigin in system/libs/workspace_ui/src/origin.ts and workspaceHostCoordinate
  // in system/libs/workspace_ui/src/app_contract.ts, which are canonical: an app origin is its label
  // prefixed onto the workspace COORDINATE -- the first host-<hex> (or, on a
  // workspace-keyed share domain, bare 32-hex share) label and everything
  // after it -- and never onto this page's host verbatim, which
  // would nest the app under the shell's own label and route back here.
  //
  // It differs from them in one way, deliberately: no coordinate label
  // means no origin, rather than falling back to the host unchanged. The shell
  // can assume it is running inside a workspace; this page cannot (a direct
  // hit on the loopback port has no coordinate), and a made-up origin would
  // show the reader a broken frame instead of the prose that still helps them.
  function appOrigin(label) {
    var labels = window.location.host.split(".");
    for (var index = 0; index < labels.length; index++) {
      // The port rides on whichever label is last, so it is stripped before
      // matching -- otherwise a coordinate that happens to BE the last label
      // reads as an ordinary one and the terminal is silently not offered.
      // The slice below keeps it, which is what the origin needs.
      if (/^(?:(?:host|agent)-[a-f0-9]+|[a-f0-9]{32})$/i.test(labels[index].split(":")[0])) {
        return window.location.protocol + "//" + label + "." +
               labels.slice(index).join(".") + "/";
      }
    }
    return null;
  }

  // Whether what the page is about is fixed, going back to the desktop if it is.
  __CHECK_RECOVERED__

  // The command is long enough to be worth not retyping, and the reader may be
  // copying it into a terminal on the far side of a share. Shown only when the
  // clipboard is actually reachable: it needs a secure context, which every
  // workspace origin is (``*.localhost`` locally, https on a share) but which a
  // direct hit on the loopback port is not.
  var copyButton = document.getElementById("copy-repair");
  if (navigator.clipboard && window.isSecureContext) {
    copyButton.hidden = false;
    copyButton.addEventListener("click", function () {
      navigator.clipboard
        .writeText(document.getElementById("repair-command").textContent)
        .then(function () {
          copyButton.textContent = "Copied";
          setTimeout(function () {
            copyButton.textContent = "Copy";
          }, 2000);
        })
        // A denied permission is the realistic failure. Selecting the text puts
        // the reader one keystroke from the same result rather than leaving a
        // button that silently did nothing.
        .catch(function () {
          window.getSelection().selectAllChildren(document.getElementById("repair-command"));
          copyButton.textContent = "Press to copy";
        });
    });
  }

  // Run one of the page's launches: the app's origin label, the page's path and its name, or a failure whose
  // ``sentence`` follows workspace_ui's failureSentence, which this page cannot import (the frontend may not be built).
  function launch(purpose, whatFailed) {
    return fetch(launchUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: reason, purpose: purpose }),
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (response.ok) return body;
        var failure = new Error(["HTTP " + response.status, body.detail || ""].join("\\n").trim());
        failure.sentence = body.user_facing_message ||
          (response.status < 500 ? whatFailed + "." : whatFailed + ". Try again. If this persists, report a bug.");
        throw failure;
      });
    }, function (error) {
      // The request may have reached the workspace before the answer was lost.
      var failure = new Error(error && error.message ? error.message : String(error));
      failure.sentence = "The workspace didn't answer, so Imbue Studio can't tell whether this went through.";
      throw failure;
    });
  }

  function pageAt(answer) {
    var origin = appOrigin(answer.label);
    return origin ? origin + String(answer.path).replace(/^\\//, "") : null;
  }

  // The terminal is a session of its own, made when the reader first opens its section rather than on every load
  // of this page or every look at the details, so a page left open or reloaded does not pile up sessions nobody
  // looks at.
  var details = document.getElementById("repair-details");
  var terminalDetails = document.getElementById("terminal-details");
  var isTerminalOffered = Boolean(terminalLabel && appOrigin(terminalLabel));
  if (isTerminalOffered) {
    document.getElementById("no-terminal").hidden = true;
    terminalDetails.hidden = false;
  }
  var isTerminalAsked = false;
  terminalDetails.addEventListener("toggle", function () {
    if (!terminalDetails.open || !isTerminalOffered || isTerminalAsked) return;
    isTerminalAsked = true;
    launch("terminal", "The workspace's terminal couldn't be opened here").then(function (answer) {
      var page = pageAt(answer);
      if (!page) return;
      document.getElementById("terminal").src = page;
      document.getElementById("terminal-slot").hidden = false;
      document.getElementById("terminal-intro").hidden = false;
    }, function (failure) {
      var line = document.getElementById("terminal-failure");
      line.textContent = failure.sentence + " (" + failure.message.replace(/\\n/g, ": ") + ")";
      line.hidden = false;
    });
  });
  // A failed ask leaves the reader the command and the terminal in the open, rather than behind "See details",
  // which keeps only why the ask failed.
  function showFallback() {
    var fallback = document.getElementById("ask-agent-fallback");
    fallback.appendChild(document.getElementById("repair"));
    fallback.appendChild(document.getElementById("no-terminal"));
    fallback.appendChild(terminalDetails);
    document.getElementById("repair-intro").hidden = true;
    fallback.hidden = false;
    if (isTerminalOffered) terminalDetails.open = true;
  }

  var reloadButton = document.getElementById("reload");
  if (reloadButton) reloadButton.addEventListener("click", returnToDesktop);
  // A row with no button in it takes no room.
  document.getElementById("page-actions").hidden = !isAgentOffered && !reloadButton;

  var askButton = document.getElementById("ask-agent-button");
  if (isAgentOffered) {
    askButton.hidden = false;
    var askLead = document.getElementById("ask-agent-lead");
    if (askLead) askLead.hidden = false;
    var askLabel = askButton.textContent;
    askButton.addEventListener("click", function () {
      askButton.disabled = true;
      askButton.textContent = "Asking an agent\u2026";
      document.getElementById("ask-agent-failure-message").hidden = true;
      document.getElementById("ask-agent-failure-reason").hidden = true;
      launch("repair_agent", "Couldn't ask an agent").then(function (answer) {
        var page = pageAt(answer);
        askButton.hidden = true;
        var started = document.getElementById("agent-started");
        started.textContent = page
          ? "An agent is on it. Follow along below."
          : "An agent is on it. Follow along in " + answer.display_name + ".";
        started.hidden = false;
        if (page) {
          document.getElementById("agent-chat").src = page;
          document.getElementById("agent-chat-slot").hidden = false;
        }
      }, function (failure) {
        // A refusal may be the shell saying there is nothing left to fix.
        checkRecovered();
        askButton.disabled = false;
        askButton.textContent = askLabel;
        var message = document.getElementById("ask-agent-failure-message");
        message.textContent = "Couldn't ask an agent. You can start one yourself: run this command in " +
          (isTerminalOffered ? "the terminal below." : "a terminal.");
        message.hidden = false;
        document.getElementById("ask-agent-failure-sentence").textContent = failure.sentence;
        document.getElementById("ask-agent-failure-details").textContent = failure.message;
        document.getElementById("ask-agent-failure-reason").hidden = false;
        showFallback();
      });
    });
  }

  // With no agent to offer, what is behind the details is the only way out, so it starts open, the terminal too.
  if (!isAgentOffered) {
    details.open = true;
    if (isTerminalOffered) terminalDetails.open = true;
  }

  setInterval(checkRecovered, __POLL_SECONDS__ * 1000);
})();
</script>
</body>
</html>
"""

_PLACEHOLDER_PATTERN: Final[re.Pattern[str]] = re.compile(r"__([A-Z_]+)__")

_RELOAD_BUTTON: Final[str] = '<button id="reload" class="action" type="button">Reload</button>'


@pure
def _recovery_check_script(reason: RecoveryReason, root_path: str) -> str:
    match reason:
        case RecoveryReason.INTERFACE_NOT_BUILT:
            return _INTERFACE_NOT_BUILT_CHECK.replace("__BUILT_HEADER__", FRONTEND_BUILT_HEADER)
        case RecoveryReason.DESKTOPS_NOT_LOADED:
            return _DESKTOPS_NOT_LOADED_CHECK.replace("__INVENTORY_URL__", json.dumps(root_path + _INVENTORY_PATH))
        case _ as unreachable:
            assert_never(unreachable)


@pure
def _failure_details_html(failure_details: str | None) -> str:
    if failure_details is None or failure_details.strip() == "":
        return ""
    return f'<pre id="failure-details">{html.escape(failure_details, quote=False)}</pre>'


@pure
def render_recovery_page(
    content: RecoveryPageContent, terminal_label: str | None, is_agent_offered: bool, root_path: str
) -> str:
    """Fill the recovery page in, given what it says, the terminal's origin label, whether an agent can be asked to
    repair what it is about, and the path prefix the shell is served under (where its launch route is).

    Takes the label rather than reading the registry itself so the page can be rendered for a workspace with no
    terminal (pass ``None``) without touching the filesystem, which is the case worth testing and the one hardest to
    arrange for real.

    The label is substituted as a JSON literal, so the script receives a string however it is spelled, and ``""`` --
    which the script treats as "no terminal" -- when there is none. The server has already rejected anything that is
    not a single DNS label, so no value that reaches here can close the script element.

    The repair command and the failure's details go in as HTML text rather than markup. The command is a shell line
    written for a reader, so ``&`` and ``<`` are ordinary characters in it that the browser would otherwise take as an
    entity reference or a tag -- which would show something other than the command, and hand the copy button (which
    reads ``textContent``) something other than what the tests validated. Quotes are left alone: this is text content,
    and the line is full of them.
    """
    ask_lead = "" if content.ask_lead is None else f'<p id="ask-agent-lead" hidden>{html.escape(content.ask_lead)}</p>'
    value_by_placeholder = {
        "CHECK_RECOVERED": _recovery_check_script(content.reason, root_path),
        "TERMINAL_LABEL": json.dumps(terminal_label or ""),
        "IS_AGENT_OFFERED": json.dumps(is_agent_offered),
        "LAUNCH_URL": json.dumps(root_path + RECOVERY_LAUNCH_PATH),
        "REASON": json.dumps(content.reason.value),
        "DESKTOP_URL": json.dumps(root_path + "/"),
        "POLL_SECONDS": str(RECOVERY_POLL_SECONDS),
        "TITLE": html.escape(content.title),
        "HEADING": html.escape(content.heading, quote=False),
        "EXPLANATION": html.escape(content.explanation, quote=False),
        "ASK_LEAD": ask_lead,
        "ASK_LABEL": html.escape(content.ask_label),
        "RELOAD_BUTTON": _RELOAD_BUTTON if content.is_reload_offered else "",
        "REPAIR_COMMAND": html.escape(content.repair_command, quote=False),
        "FAILURE_DETAILS": _failure_details_html(content.failure_details),
    }
    # One pass, so nothing substituted (a failure's details, a command carrying them) is read as a placeholder.
    return _PLACEHOLDER_PATTERN.sub(lambda match: value_by_placeholder[match.group(1)], _RECOVERY_PAGE_TEMPLATE)
