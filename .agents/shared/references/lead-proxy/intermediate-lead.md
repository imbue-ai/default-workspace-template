# Lead-side proxy flow: when you are a worker yourself

Everything in the lead-proxy flow (`.agents/shared/references/lead-proxy.md`)
holds unchanged for an intermediate lead: a worker that launched its own
worker. Four rules are yours alone.

- **Your worker's `question` is yours to answer first.** Answer it yourself
  whenever your own task file and the repo settle it -- that is most of them.
  Only when the answer is genuinely not available to you do you re-raise it as
  your *own* `question` gate to your lead (`create_worker.py report --type gate
  --name question`, the way you report everything else), and when the reply
  comes back you forward it to your worker **verbatim** with `mngr message`. Do
  not paraphrase a decision you did not make.
- **Merge exactly one level.** Your worker's branch `mngr/<its-name>`
  merges into *your* branch, with `--no-ff` and the worker named in the
  merge commit message. Your own lead then sees one merged branch and never
  needs to know you launched workers.
- **Destroy after merge, stop on failure.** Once your worker's branch is
  merged, destroy it (`create_worker.py destroy --name <its-name>`);
  a stuck one is stopped (`create_worker.py stop --name <its-name>`)
  after the failure flow. Never destroy yourself: your own lead does that
  once it has merged you. This matters for your own lead too: a merged
  worker of yours left in WAITING still reads as a live worker of yours, which
  keeps you counted as busy after you have finished, while a STOPPED one does
  not.
- **Await your workers with a shorter timeout than your own lead awaits you
  with** (`--timeout 60m` under a lead's 90m), so yours ends inside its window.
