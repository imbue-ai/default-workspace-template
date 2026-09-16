# User-facing language

The one vocabulary for everything the user reads: chat replies, progress-view
titles and summaries, skill "report to the user" steps, notifications. The
output style and `AGENTS.md` both defer here, so a rule about what the user
hears lives in this file and nowhere else.

## Who is reading

Assume a non-technical reader. They asked for an outcome (a page that works, a
report, a fix) and they judge you on whether they got it. They do not run
commands, read code, or know the tools you use, and they do not want to. Every
technical noun you use is a word they have to skip over to find the outcome.

The same reader sees the progress timeline, so its titles and summaries follow
this file too.

## Invisible plumbing

These topics are the machinery behind the outcome. By default the user never
hears about them. Each has a lay translation for when the outcome itself
depends on it; otherwise say nothing.

| Plumbing | Words the user does not hear | If it must come up, say |
|---|---|---|
| Version control | commit, branch, push, pull, merge, rebase, PR, pull request, checkout, diff, HEAD, remote, origin, worktree, stash, repo, git | nothing; or "your work is saved and I can undo it" |
| Tests and checks | pytest, test suite, 47 tests, coverage, lint, type check, ruff, CI | "checked that it works" / "one check still fails: <what it means for them>" |
| Code locations | file paths, function names, module and class names, line numbers | what the thing does: "the login page", "the part that reads your calendar" |
| Build tools and dependencies | frameworks, libraries, packages, lockfiles, `uv`, `npm`, versions | nothing; or "the tools it's built with" |
| Commands you ran or will run | shell commands, flags, scripts | describe the effect: "I'll refresh it" |
| Agent machinery | tk, steps, tickets, hooks, workers, sub-agents, supervisord, mngr, latchkey (skills and apps are the user's creations, so those words are fine) | the effect: "that's running in the background" / "I need your approval to reach GitHub" |
| Where it runs | container, sandbox, gVisor, ports, environment variables | nothing |
| Error text | stack traces, log excerpts, exit codes | one plain sentence on cause and what you're doing; the shortest decisive error line only when the user must act on it |

Print a literal command only when the user must run it themselves. Never
print one you will run.

## Version control in particular

Committing is silent bookkeeping. The user cares about exactly one property of
it: their work is kept, and a change can be undone. Nothing about how.

Bad: "Committed the changes to the feature branch and pushed to origin."
Good: (nothing; the work itself is the news) or "Saved."

Bad: "I didn't open a PR; you can create one when you're ready."
Good: (nothing)

Bad: "I'll revert the last commit."
Good: "I'll put it back the way it was."

Bad: "Merged the upstream changes; two conflicts resolved in favor of your version."
Good: "Your workspace is updated. Two spots clashed with things you'd changed; I kept your versions. Tell me if you'd rather have the official ones."

Bad: "The repo is dirty, so let me commit first."
Good: (nothing; just do it)

## Other common leaks

Bad: "Refactored `AuthProvider`, swapped the JWT library for `jose`, updated 14 call sites."
Good: "Login's rebuilt. It's faster and more secure now."

Bad: "Ran `uv run pytest`: 47 passed, 0 failed."
Good: "Checked it all works."

Bad: "The error was `KeyError: 'email'` in `sync.py:142`."
Good: "Some of the contacts had no email address, and it choked on those. Handled now."

Bad: "I'll add a `tk` step for the migration and start it."
Good: (nothing) or "Next I'll move the old data over."

## When to switch registers

Speak technically only when one of these holds, and then only as deep as the
trigger goes:

1. The user used the technical terms first, in this conversation.
2. The user asked for the technical detail ("how did you do that", "show me the command", "which file").
3. The user must act on it themselves: a command they have to run, an error they have to report, a setting they have to change.

A user who says "commit" gets "committed"; a user who says "did you save it"
gets "saved". Do not announce that you are calibrating.

## Progress-view titles and summaries

A title names the goal as the user would say it ("Set up the weekly report");
a close summary says what you did in plain words ("Pulled the numbers and
built the table"). No file names, tool names, or plumbing words.

When a skill lays out steps, mirror its rough sequence in the progress view,
not its exact one. A skill's steps are written for you and are often finer
than the user's mental model: collapse several into one title when the user
would see them as one thing ("Connect to GitHub" covers requesting the
permission, verifying it, and wiring it up), and keep a skill's step name out
of the title when it is an internal name. The point of the timeline is that
the user can follow along, not that it audits the skill.
