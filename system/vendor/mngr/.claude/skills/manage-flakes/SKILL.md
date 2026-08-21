---
name: manage-flakes
description: Given a set of flaky tests (full-window or partial), reconcile the MIND Linear backlog -- cluster by root cause, prioritize via the branch filter, and create/update/close flake tickets. The reusable core; detect-flakes (full CI sweep; may close stale tickets) and report-incidental-flakes (one flake an author hit; create/update only) both apply it. Usually invoked by those skills, not directly.
---

# Manage flakes into MIND tickets

Take the set of flaky tests the caller hands you and make the MIND Linear backlog reflect it. **You supply the judgment** -- clustering, matching, and state decisions; the `scripts/flake_reconcile.py` CLI only moves data and never clusters. One ticket per root cause: never per test, never per symptom.

## What the caller gives you

- A list of flaky tests in the shape the CLI's `list-flakes` emits -- at least `test`, `sample_failure_lines`, `is_marked_flaky`, `branches`, `last_seen`. (A single-flake caller assembles one such record.)
- Whether the set is **full-window** (every flake in a CI window, e.g. `detect-flakes`) or **partial** (e.g. one incidental flake from `report-incidental-flakes`). This flag gates closing -- see step 4.

Precondition: `latchkey services info linear` reports `"valid"`.

## The CLI

All ticket reads and writes go through `uv run python scripts/flake_reconcile.py <cmd>` from the repo root (`--help` for flags):

- `list-tickets` -- existing flake tickets as JSON (read-only)
- `preferred-status --branch <b> ...` -- the branch filter; prints `ready`/`backlog`/`unknown`
- `create-ticket --title ... --body-file ... --status ready|backlog`
- `update-ticket --id <issue_id> --body-file ... [--title ...]` -- replaces body (and optionally title); never touches state
- `set-status --id <issue_id> --status ready|backlog`
- `comment-ticket --id <issue_id> --body-file ...`
- `close-ticket --id <issue_id>`

Write bodies and comments to temp files so multi-line markdown survives. Nothing is written until you run a write command -- and never before step 5's approval.

## The loop

### 1. Read the existing tickets

`list-tickets > /tmp/tickets.json`, then read it. Each ticket has `identifier`, `issue_id`, `is_open`, and `description` (the full body, including markers written by previous runs).

### 2. Cluster the flakes -- for resolution, not description

Cluster with exactly one goal: **each ticket is a handoff unit one future fixer can resolve as a single coherent piece of work.** Before committing to a cluster, ask: could one fixer, with one capability, reproduce and fix all of this in one focused effort? If the fix for test A is unrelated to the fix for test B, they are different tickets even when the symptom matches.

- **Group by root cause and its fix, never by shared symptom.** Read the failure lines and group by understanding, not string matching. (Illustration: three tests that all "time out" may be three tickets -- three unrelated slow paths -- while dozens of tests failing at the same infrastructure bring-up step are one ticket, because one fix hardens that step for all of them.)
- **Occurrence counts are severity signal for the body, never a clustering criterion.** Do not bundle unrelated one-offs to keep the ticket count down; a one-off with a distinct cause is its own (low-priority) ticket, and staleness (step 4) retires it if it never recurs.
- **Refuse defensibility and inertia.** Do not accept a grouping merely because it is plausible, and never keep a cluster as-is just because it is already filed -- that dumps several unrelated investigations on one fixer under one title. When in doubt, split: independently-actionable beats tidy.

For each cluster settle on: a title naming the cause, the affected tests, a one-line **resolution hypothesis** (what fixing it looks like, and which subsystem it lives in), severity from the occurrence counts, and a callout of any affected test lacking `@pytest.mark.flaky` (unmarked flakes turn CI red).

### 3. Prioritize each cluster -- the branch filter

A flake seen only on one unmerged feature branch is probably that branch's own bug; filing it as ready sends a fixer chasing something they cannot reproduce on `main`. Take the union of `branches` across the cluster's tests. The rule is deterministic -- `preferred-status` computes it:

- any flake on **`main`** -> **ready** (a live problem on main)
- never on main, but on **more than one** feature branch -> **ready** (branch-independent / systemic; it will reach main)
- only ever on **a single unmerged feature branch** -> **backlog** (likely branch-local; the branch's subject matching the failing test's domain is near-proof)
- no branch evidence -> **unknown**: leave status alone

### 4. Reconcile against existing tickets

Every body carries a stable `flake-cluster` slug and a `last-seen` timestamp (see "Ticket body" below). Match each cluster to an existing **open** ticket by slug -- or by clear semantic overlap when a ticket predates the markers -- then decide:

- Cluster with no open ticket -> **CREATE**, at its preferred status.
- Cluster with an open ticket -> **UPDATE** the body (refresh the affected-tests table and `last-seen`).
- Open ticket whose cluster is absent from the current set **and** whose `last-seen` is more than **21 days** before today -> **CLOSE** as stale. Compute the cutoff by plain date math against today.

**Close only on a full-window sweep.** A partial caller cannot know that anything stopped flaking: it may CREATE and UPDATE, never CLOSE. A repeated full sweep with no new flakes should produce no changes.

**Never clobber a human or agent move.** Change an existing ticket's state only while it is still in a pre-work state (Todo/Backlog) and its preferred status differs -- then `set-status`. In Progress / In Review / Done / Canceled means someone moved it intentionally: leave its state alone -- never demote an In Progress ticket, never reopen a Done/Canceled one. On every state change you make, `comment-ticket` explaining why, citing the branch evidence. If a backlog-preferred cluster is already In Progress, do not demote it -- comment that it has only flaked on one unmerged branch, so the assignee knows it may not reproduce on main.

**Recluster freely as new information arrives.** Clusters are hypotheses, not commitments; do not leave a wrong grouping standing out of inertia.

- **Split:** `update-ticket` (with `--title`) to re-scope the existing ticket down to one cause, then `create-ticket` for each part that split off.
- **Merge:** `update-ticket` the survivor to cover the whole cause, then `close-ticket` the now-redundant ticket(s) (full-window callers only).

When you re-scope a ticket, give it a new `flake-cluster` slug matching its new shape so future runs dedup correctly.

### 5. Show the plan, then apply

Summarize every intended CREATE / UPDATE / CLOSE / state change for the user and **get approval before the first write**. On approval, execute and report what changed (each command prints the affected ticket).

## Ticket body

Sibling skills (the fixer and the reporters) parse this shape -- keep it. At the top of every body:

```
<!-- flake-cluster: <short-stable-slug> -->
<!-- last-seen: <ISO-8601 of the cluster's max last_seen> -->
```

Then, skimmable: the root cause and resolution hypothesis; an affected-tests table (`test | flaking commits | hard-fails | @flaky`); the representative failure line(s); an unmarked-flake callout if any; and how many commits/days the cluster spans. Footer it as auto-filed by `manage-flakes`.
