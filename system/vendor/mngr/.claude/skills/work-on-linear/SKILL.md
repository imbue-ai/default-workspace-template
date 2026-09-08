---
name: work-on-linear
description: |
  Work end-to-end on a Linear ticket inside a Sculptor workspace: fetch and
  understand the ticket, gate on its workflow state, claim it (assign to the
  user + move to In Progress), set up a properly named branch, implement it
  following repo conventions, and stop for local review without opening a PR.
  Use when given a Linear issue identifier (e.g. ENG-123), a Linear issue URL,
  or when asked to "work on" / "pick up" / "start" a Linear ticket. Also handles
  `new` (`/work-on-linear new [TEAM] [prompt]`) when no ticket exists yet:
  infer the current work, search for overlapping tickets, create one, then
  continue the same flow.
  Not project-specific. Requires latchkey with Linear credentials.
---

# Work on a Linear Ticket (`/work-on-linear`)

Picks up a Linear ticket and carries it to completion in the current Sculptor
workspace. The workflow is gated: unclear tickets get clarified first, tickets
in the wrong state get confirmed first, and finished work stops for local
review — never a PR.

There are two ways in, and they converge. Given a ticket identifier, the skill
adopts that ticket (Step 1 onward). Invoked as `new`, it first *creates* a
ticket for work that has none (the **Entry `new`** section below), then falls
into the same single track. Once the ticket is settled, the entry point no
longer matters — there is one path from there on.

## Usage

```
/work-on-linear ENG-123
/work-on-linear https://linear.app/<team>/issue/ENG-123/some-title
/work-on-linear new [TEAM] [prompt]
```

If invoked with an identifier or URL, adopt that ticket (Step 1 onward). If
invoked as `new`, create a ticket first (see **Entry `new`** below), then
continue the same flow. If invoked bare — no identifier and no `new` — ask the
user which ticket to work on, or whether they meant `new`.

## Preconditions

- Linear access goes through the `latchkey` skill. All API calls use
  `latchkey curl` against `https://api.linear.app/graphql`.
- Verify credentials are viable first:
  ```bash
  latchkey services info linear   # credentialStatus should be "valid"
  ```
  If not valid, follow the latchkey skill to set them up
  (`latchkey auth browser linear` or `latchkey auth set linear -H "Authorization: <token>"`)
  before continuing.

## Entry `new` — no ticket exists yet

Invoked as `/work-on-linear new [TEAM] [prompt]`. Use this when the user is
already doing work that has no Linear ticket. This entry *creates* the ticket,
then falls into the single track below.

`new` subsumes Steps 1–3: by the end of it you have a well-understood ticket
that is assigned to the user and In Progress. Do **not** then re-run the state
gate (Step 2) or re-claim (Step 3) — continue at **Step 4**. If this entry does
not apply (you were given an identifier), skip straight to Step 1.

### 0a — Determine what the user is working on

Build a picture of the current logical unit of work from, in order:

1. The `[prompt]` argument, if given — the user's own description wins.
2. The working tree: `git status`, `git diff` (staged + unstaged), and the
   current branch name.
3. Commits on this branch not yet on its base (`git log`), and recently edited
   files.

Synthesize **one** logical problem — the ticket should cover a single coherent
change, not the sum of every unrelated edit in the tree. If the workspace shows
several unrelated threads, or you cannot tell what the work is, STOP and ask the
user to describe the ticket rather than guessing.

### 0b — Resolve the team

The ticket needs a team. Resolve in order:

1. The `[TEAM]` argument (a team key like `ENG`, or a name), if given.
2. Infer from the repo: an existing convention — branch prefixes referencing a
   team key, prior tickets, a documented default team.
3. If still ambiguous, list the user's teams and ask.

```bash
latchkey curl -X POST https://api.linear.app/graphql \
  -H 'Content-Type: application/json' \
  -d '{"query":"query { teams { nodes { id key name } } }"}'
```

Keep the team's UUID; you will also need its In Progress state id (the states
query in Step 3).

### 0c — Search for overlapping tickets (caution gate)

Before creating anything, search Linear for a ticket that may already cover this
work — a duplicate is worse than reuse:

```bash
latchkey curl -X POST https://api.linear.app/graphql \
  -H 'Content-Type: application/json' \
  -d '{"query":"query { searchIssues(term: \"<keywords>\", first: 10) { nodes { identifier title url state { name type } assignee { displayName } team { key } } } }"}'
```

`searchIssues` matches loosely (any term) and is unranked-by-relevance, so
always cap it with `first:` and judge the hits yourself — most will be
noise. Search a few keyword variations drawn from the scope, and weight matches
by how well the title/team fit *and* by state: a same-team ticket in an
actionable state (`unstarted`/`started`) is a real overlap; long-closed
(`completed`/`canceled`) or unrelated-team hits usually are not. If a plausible
live match exists, STOP and present it. Ask whether to **adopt the existing
ticket** (drop into the normal flow on it, starting at Step 1) or **create a new
one anyway**. Do not create silently over a likely duplicate.

### 0d — Draft, confirm, then create

Draft the ticket: title, description (what and why; acceptance criteria if
clear), team, and priority if evident. Creating a Linear ticket is
outward-facing and awkward to undo, so **show the draft and the overlap-search
result to the user and get explicit confirmation before creating it.** This is
also where a missing or thin `[prompt]` gets corrected — propose, and let the
user amend.

On confirmation, create the ticket already assigned to the user and In Progress
(this folds in Step 3's claim; get the viewer id and In Progress state id as in
Step 3):

```bash
latchkey curl -X POST https://api.linear.app/graphql \
  -H 'Content-Type: application/json' \
  -d '{"query":"mutation { issueCreate(input: { teamId: \"<TEAM_UUID>\", title: \"<title>\", description: \"<description>\", assigneeId: \"<VIEWER_UUID>\", stateId: \"<STARTED_STATE_UUID>\" }) { success issue { id identifier url state { name } assignee { displayName } } } }"}'
```

Record the returned `issue.id` and `identifier`, then continue at **Step 4**.

## Step 1 — Fetch and understand the ticket

Extract the issue identifier (e.g. `ENG-123`) from the argument or URL, then
fetch the ticket:

```bash
latchkey curl -X POST https://api.linear.app/graphql \
  -H 'Content-Type: application/json' \
  -d '{"query":"query { issue(id: \"ENG-123\") { id identifier title url description state { id name type } assignee { id displayName email } creator { displayName } team { id key name } priorityLabel labels { nodes { name } } branchName comments { nodes { body user { displayName } createdAt } } children { nodes { identifier title state { name type } } } } }"}'
```

Read the title, description, comments, labels, and sub-issues. Then decide:

- **Clear enough to start?** The ticket has a well-scoped goal and you can
  identify what "done" looks like (explicit acceptance criteria, or an
  obvious, uncontroversial interpretation).
- **Not clear?** STOP. Do not guess. Ask the user a blocking question naming
  exactly what is missing or ambiguous (scope, acceptance criteria, which of
  several interpretations to take, conflicting comment threads). Only proceed
  once the direction is settled. If the user amends the requirements,
  incorporate that into your understanding before touching code.

## Step 2 — Gate on the ticket state

Use `state.type` (not the display name, which varies per team):

| `state.type`              | Action |
|---------------------------|--------|
| `unstarted` (Todo/Ready)  | Proceed to Step 3. |
| `backlog` or `triage`     | STOP and ask for confirmation: the ticket has not been groomed/scheduled. Proceed only on explicit approval. |
| `started` (In Progress)   | Check whether THIS workspace is already the one working on it: does the current branch name contain the issue identifier, or do uncommitted/recent commits reference it? If yes — this is a continuation, proceed to Step 5 (skip re-claiming). If no — someone or something else may be mid-flight; STOP and ask for confirmation before taking it over. |
| `completed` or `canceled` | STOP and ask. Do not silently resurrect finished work. Report the state and ask whether the user wants to reopen it, file a follow-up instead, or abort. |

Also note the current assignee: if the ticket is assigned to someone other
than the user, mention that when asking for confirmation or before claiming.

## Step 3 — Claim the ticket

Only after the state gate passes (and the user confirmed where required):

1. Get the user's own Linear identity:
   ```bash
   latchkey curl -X POST https://api.linear.app/graphql \
     -H 'Content-Type: application/json' \
     -d '{"query":"query { viewer { id displayName email } }"}'
   ```
2. Find the team's In Progress state id (pick the `started`-type state named
   "In Progress", else the lowest-position `started` state):
   ```bash
   latchkey curl -X POST https://api.linear.app/graphql \
     -H 'Content-Type: application/json' \
     -d '{"query":"query { team(id: \"<TEAM_UUID>\") { states { nodes { id name type position } } } }"}'
   ```
3. Assign to the user and move to In Progress in one mutation:
   ```bash
   latchkey curl -X POST https://api.linear.app/graphql \
     -H 'Content-Type: application/json' \
     -d '{"query":"mutation { issueUpdate(id: \"<ISSUE_UUID>\", input: { assigneeId: \"<VIEWER_UUID>\", stateId: \"<STARTED_STATE_UUID>\" }) { success issue { identifier state { name } assignee { displayName } } } }"}'
   ```

Use the issue's UUID (`issue.id`), not the identifier, for mutations.

## Step 4 — Set up the branch and workspace

Sculptor workspaces run on their own branch. Name it so the work is
traceable back to the ticket.

**On first invocation for a ticket** (new work, not a continuation),
also rename the Sculptor workspace itself so it is identifiable in the
Sculptor UI:

```bash
sculpt workspace rename "$SCULPT_WORKSPACE_ID" "<IDENTIFIER>: <short ticket title>"
```

Use the ticket identifier plus a shortened form of the ticket title
(e.g. `OFFLOAD-4: venv activation for pytest command`). Skip this when
`SCULPT_WORKSPACE_ID` is unset (not running inside Sculptor) or when the
workspace already has a meaningful name for this ticket.

**New work (branch not yet named for this ticket):** rename the current
branch:

```
<user>/<IDENTIFIER>-<short-kebab-description>
```

- `<user>`: the user's handle — prefer the prefix they already use on other
  branches (`git branch -a`), else their Linear display name or
  `git config user.name`, first token, lowercased.
- `<IDENTIFIER>`: the ticket key, e.g. `ENG-123`.
- `<short-kebab-description>`: 3–6 words from the ticket title, lowercased,
  hyphenated, no filler words.

Example: `git branch -m danver/ENG-123-fix-login-redirect-loop`

If Linear's `issue.branchName` suggests a different name and the repo has no
stronger convention, prefer the user's convention above; consistency with the
user's own branches wins.

**Continuing work:** if a branch for this ticket already exists locally or on
the remote and the current branch is not it, STOP and tell the user — do not
silently checkout or duplicate an in-flight branch. Ask whether to switch to
it, rebase onto it, or start fresh.

## Step 5 — Implement

- Follow the repository's own conventions first: read `AGENTS.md`,
  `README.md`, style/testing guides, and any project instructions in the
  workspace before writing code. User-specific conventions or overrides
  stated in this session take precedence over repo defaults.
- Keep changes scoped to the ticket. No drive-by refactors.
- Make atomic commits on the ticket branch. Reference the identifier
  (e.g. `ENG-123`) in commit subjects where the repo's commit policy allows.
- **If you hit an interruption, ask for help.** Missing credentials, a
  dev environment that won't start, a requirement that turns out to be
  contradictory, a dependency on someone else's unfinished work — stop and
  ask a blocking question rather than guessing, working around silently, or
  abandoning the task.
- If the ticket grows beyond its original scope mid-work, surface that and
  confirm before expanding.

## Step 6 — Finish: verify locally, then STOP for review

When the implementation is complete:

1. Run the repo's local verification: its test suite, linter, type checker,
   and build — whatever the repo's docs/config define. All must pass; fix
   failures that are attributable to your change.
2. Report a concise summary: what changed, how it was verified, and anything
   the reviewer should look at closely.
3. **Do NOT create a pull request. Do NOT push without explicit permission.**
   The work stops here for local review by the user.

Only if the user explicitly asks, follow up actions may include pushing the
branch, opening a PR, or moving the Linear ticket to the team's review/done
state. When you do open a PR, write its title and description in the voice below.

## Writing the PR title and description

Applies only when the user asks you to open a PR. Write for a teammate skimming a
long list of PRs. Plain English: short, direct sentences (Hemingway); lead with
the outcome (patio11). Explain any name a newcomer would not know.

### Title

Format: `[<IDENTIFIER>] <text>` (e.g. `[MIND-229] ...`). Lead with the ticket tag
whenever the work has a ticket; if it genuinely has none, drop the tag entirely —
no `[<no-ticket>]` placeholder. `<text>` names the subject and outcome — what got
better, and where — in words a teammate would say out loud.

Open `<text>` with a category label only when a strong, established one applies:
`Deflake:` (tied to the flake-fixing process), with `Sentry:` coming soon. Skip
it otherwise — weak labels like `Fix:` or `Refactor:` earn nothing.

Two failure modes, with real examples from one deflake PR:

- **Jargon salad** — internal identifiers strung together, reading like a code
  path: `deflake list-agents continue-mode provider-error tests`.
- **Coy / information-free** — a count or symptom with no subject, so the reader
  must open the PR to see what it touches: `Fix three tests that randomly turned
  CI red`.

Target: `[MIND-229] Deflake: Make listing agents resilient to provider failures`.

### Description

Plain English, short paragraphs. In order:

1. **What was wrong** — the symptom (CI went red, the command crashed).
2. **Why** — the root cause in everyday terms; explain unfamiliar names (e.g.
   "Lima runs VMs on your machine").
3. **What you changed** — including what you did not change (e.g. "tests only;
   the command behaves as before").
4. **How you checked** — the checks you ran, with numbers.

Skip the line-by-line diff, unexplained identifiers, and hype ("massive win").

## Quick reference

| Need | Endpoint / field |
|------|------------------|
| Issue by human key | `issue(id: "ENG-123")` (identifier works) |
| Search for duplicates | `searchIssues(term: "<keywords>", first: 10)` — loose match, cap + rank yourself |
| Create a ticket (`new`) | `issueCreate(input: { teamId, title, description, assigneeId, stateId })` |
| List teams | `teams { nodes { id key name } }` |
| Mutation target | issue UUID (`issue.id`), not the identifier |
| State machine | `state.type`: `triage` / `backlog` / `unstarted` / `started` / `completed` / `canceled` |
| Team states | `team(id:) { states { nodes { id name type position } } }` |
| The human owner | `viewer { id displayName email }` |
| Suggested branch | `issue { branchName }` |
| Auth | `latchkey curl` (see the `latchkey` skill) |
| API docs | https://linear.app/developers/graphql |
