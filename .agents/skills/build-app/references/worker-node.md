# Working as one node of an app build

You are one node of a plan for building an app. An orchestrating agent launched
you and several other workers, each into a git worktree of its own, branched
from the build's branch as it stood when you started -- so every node merged
before you is already in your checkout, and the work you do is handed back by
being committed to your branch. Your task file names your subtask and gives you
the reports of the nodes you depend on, and its frontmatter names its own path
and where your report goes. This document is the rest of your task.

Read `.agents/skills/build-app/references/worker-workspace-rules.md`
too. It is the subset of the workspace's `AGENTS.md` that applies to a worker --
`AGENTS.md` itself is loaded into your context automatically and is written for
the agent holding the chat with the user, so where the two differ, that file is
the one that applies to you. This document says how to work as a node; that one
says how the workspace works.

## Do only your subtask

Your subtask is the whole of your job. The rest of the app is other nodes' work:
some are building it right now beside you, and some start after you report.
Doing any of it yourself collides with them, or builds a second version of
something the plan already assigns.

1. **Do exactly what the subtask asks, and stop.** When its hand-back is ready,
   report. Do not go on to the next thing the app needs, however obvious or
   quick it looks.
2. **Leave out everything the subtask does not name.** If it does not say to
   scaffold, do not scaffold. If it does not say to build the mock, the page,
   the routes, the storage or the icon, do not build them. Do not wire pieces
   together, verify the whole app, add tests, or tidy, refactor or restyle files
   you were not given.
3. **If something you need is missing, report `stuck`.** An earlier node's output
   that is not in your checkout, or a contract its report does not give, is a
   problem for the orchestrator -- it may not have merged that node yet, and it
   is the one that can fix that. Do not build the missing piece yourself.
4. **If the subtask looks wrong or incomplete, do it as written** and say in your
   report what you would change. Do not widen it.

## Your worktree and your branch

The checkout is yours alone; the other workers have their own. What you commit
to your branch is the only thing that reaches the build, and the orchestrator
merges it the moment you report.

1. **Edit only the files your subtask gives you.** Your subtask names what you
   own (a module, the page template, the static files). Create new files freely
   inside that boundary. If the work genuinely needs a change to a file outside
   it, do not make the change: describe it in your report and let the
   orchestrator schedule it. A file two nodes both write becomes a merge
   conflict for the orchestrator to sort out, which costs the build more than
   the edit saved you.
2. **Commit your own work, and touch no other git state.** `git add` and
   `git commit` on your own branch, as many commits as you like, and everything
   committed before you report. No `stash`, `checkout`, `switch`, `reset`,
   `rebase`, `merge`, or any command naming another branch: the orchestrator
   owns the build branch and every other node's. Reading (`git status`,
   `git diff`, `git log`) is fine.
3. **Leave the two shared files alone** -- the root `pyproject.toml` and
   `uv.lock` -- unless your subtask is the one that scaffolds the app or adds its
   libraries. If it is, run `uv sync --all-packages` after changing them. Every
   node's branch carries these files, so a second node editing them is a
   conflict at merge time. The app's own manifest and supervisord program file
   belong to the app, not to the workspace, so they are yours if your subtask
   covers them.
4. **Do not touch the running workspace.** Apps run under supervisord from the
   workspace's main checkout, not from your worktree. Do not run `supervisorctl`,
   `system/scripts/forward_port.py` or `system/scripts/layout.py`, and do not
   open windows. The orchestrator shows the user a preview and takes the app live
   after the build.

## How to build

`.agents/skills/build-app/references/app-building-guidance.md` describes how a whole app is built here, from
the first question to the hardening handoff. Use it only as a reference for *how*
to do your subtask: the scaffolder, file-path conventions, the `frontend-design`
skill before any markup, raw-data affordances. It never tells you *what* to do.
Read the parts that cover your subtask and follow their mechanics; ignore its
order of steps and everything outside your subtask. Its steps for the main agent
-- running the plan recorder, asking the user questions, showing the mock,
surfacing the window, and handing off to `crystallize-creation` -- are never yours.

Where you meet something you would rather ask about (a name, a default, an
ambiguity), decide, and say what you decided in your report.

You never talk to the user, and you never start anything that asks the user for
something, such as a `latchkey` permission request. Every contact with the user
is a separate node the orchestrator runs.

## How to check your work

One command, and only one: whatever proves the files you wrote load. For Python
that is importing the module you added (`uv run python -c "import <module>"`);
for a page or a static asset it is that the file parses. Then report.

**Build nothing to check with unless your subtask asks you to.** No test suite,
no throwaway script that drives your piece, no serving the app and loading it
with curl or Playwright, and none of the full test suite, coverage, ratchets,
`/autofix` or any review gate, even where `CLAUDE.md` asks for them. In a build
this instruction used to ask every node for a served check, and four of five
workers answered it by writing their own `check_*.py` -- between a third and two
thirds of each worker's time, which found nothing that mattered.

What that check would have caught is already caught, later and in one place: the
orchestrator serves a preview of the whole app at each review, in front of the
user, and one hardening pass runs the real tests once everything is built. Your
job is to hand over a piece that loads and a report that says what you built.

**Unless checking is the subtask.** A plan can name a node whose whole job is to
verify -- that the app serves and renders, that a behaviour holds, sometimes
that there are tests for it -- and to diagnose what that turns up. If that is
your task file, the rules above do not apply to it: do the checking, because it
is the work. Write and run the tests your subtask asks for and no others,
following the conventions of the tests already sitting beside the code you are
changing. The full suite, coverage, ratchets and the review gates stay with the
hardening pass at the end, whatever your subtask is.

A node that drives the app drives it headlessly, the way
`worker-workspace-rules.md` describes under "Important commands and
conventions". Never the browser fleet: it streams a browser to a pane for the
user to watch and take over, and that is a conversation the orchestrator owns.

## Two hooks that will refuse your commands

Both refusals cost you a turn, and both are easy to avoid:

- **Never pipe anything through `tail` or `head`** to shorten it. Redirect to a
  file and read the file instead.
- **A `tk` command must be the only thing in its tool call** -- no `cd` in front,
  nothing chained after it with `&&` or `;`, no redirect.

## Reporting back

Commit before you report -- an uncommitted change is one the orchestrator cannot
merge, and it will read as a node that did nothing.

Then follow `.agents/shared/references/worker-reporting.md`: read your task file's
stamped paths, write your report body to a file, and hand that file to the
launcher's `report` subcommand, which delivers it to the orchestrator. Your
flow's only report values are `--type status` with `--name done` or
`--name stuck`. That file tells you to address the user; in this flow the reader
is the orchestrator and the nodes after you, and the shape below is what they
need.

The body of a `done` report is the handoff the nodes after you read, and it is
pasted whole into the task file of every node that depends on you, so keep it
short for their sake.

Which of the three shapes below you use follows from what your subtask hands
over. Each names the one part with no word target -- the part the next node
cannot work without -- and a rough target for the rest.

### If your primary responsibility was a decision, a spec or a contract

Nothing you produced is in the folder, so the report is the deliverable.

- **The contract itself** -- fields and their types and rules, function
  signatures, routes and what they return, ordering, validation, the states a
  page must cover. No target: say all of it.
- **Decisions the contract does not make obvious**, with the reason in the same
  line, only where a node would otherwise pick differently.
- **What you deliberately left open** for a later node to settle.
- **The paths the contract turns on** -- where the module, asset or route it
  describes should live, and the path of anything you did write, such as a
  contract file or a fixture. One per line, so no later node has to guess a
  location you had in mind.

Around 150 words beyond the contract. Nothing to run, so no command.

### If your primary responsibility was implementation

The code is in the folder, so name the parts of it that cannot be guessed.

- **The public interface a later node calls** -- exported names, routes and
  what they return, element ids, data shapes, the app name, package folder and
  port. A list of the things themselves, not prose about them. No target: be
  complete.
- **What you built** -- three sentences, no subheadings.
- **Files you created or changed** -- the paths, one per line, nothing said
  about each.
- **Where you departed from the contract you were given**, and any decision a
  later node could contradict -- one line each. A field you capped at 500
  characters is one; your palette and your copy are not, because they are in the
  files.
- **What you left stubbed**, and anything you need changed outside your
  boundary -- one line each.
- **How to run it** -- the command, and the branch your work is on. Nothing
  about what you saw.

Around 300 words beyond the interface.

### If your primary responsibility was checking, testing, verification or validation

Your findings are the deliverable, and a failure is worth more than a pass.

- **What failed** -- for each: what you did, what you expected, what happened,
  and the file and line it comes from. No target: enough for the next node to
  fix it without repeating your work.
- **What you exercised** -- the commands and the scenarios, as a list.
- **What held** -- one line for the lot.
- **What you did not cover**, and why.
- **Tests you added** -- the paths.

Around 200 words beyond the failures.

Use `stuck` when you cannot finish the subtask: say why in one or two sentences
and what you would need.

## After a `done` report

Stop your turn. If you built something the user reviews (the mock or the working
site), the orchestrator may message you with changes the user asked for. Apply
them inside your boundary, check them, **commit them on the same branch**, and
deliver a fresh report the same way; the orchestrator merges your branch again.
A second report is shorter than the first: the same six items, carrying only the
lines that changed.
