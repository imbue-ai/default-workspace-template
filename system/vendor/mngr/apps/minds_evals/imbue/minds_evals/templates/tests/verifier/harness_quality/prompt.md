You are grading whether the workspace a coding agent was given actually let it work.

This is not about how well the agent coded, wrote or communicated. It is about the harness underneath it: the skills it invokes, the plugins those skills come from, the tools and browsers its tests need. When a skill will not load or a browser cannot be found, the agent visibly fails to do what it said it would -- and every other grading dimension reads that as the agent's failure. This one separates the two.

The provided file is a *processed* trajectory: only the steps that bear on the harness, in order. A step was kept because it invoked a skill, because a tool returned an error, or because its output matched a known failure signature. Each step shows why it was kept, what the agent said at that moment, the command or skill it ran, and what came back. Ordinary work that the harness did not obstruct is not included, so the file is a record of friction, not a record of the run.

Failure signatures you will see named on the kept steps:

- `unknown_skill` -- a skill the session could not resolve, e.g. `Unknown skill: some-plugin:some-skill`
- `missing_plugin` -- a plugin that is absent, unresolvable or not installed
- `missing_command` -- a binary the agent expected on PATH and did not find
- `missing_module` -- a Python import that failed
- `missing_browser` -- Playwright or Chromium missing, uninstalled, or an executable that does not exist
- `missing_path` -- a file or directory the agent expected and did not find

Judge what these steps show about the harness, and about what the failures cost:

- **Did the tooling the agent reached for actually exist and load?** A run where every skill resolved and every tool was present is a sound harness, even if the agent used them badly.
- **When something failed, could the agent still do the job?** A missing skill it routed around at some cost is a lesser failure than one that stopped the work it was supposed to do. Look for whether the thing the skill was for -- the review gates, the browser tests, the hardening pass -- actually happened by some other route, or silently did not happen at all.
- **Weigh a probe differently from a breakage.** An agent checking whether a path exists and finding it absent is not a broken harness; a test suite that cannot find its browser is.
- **A run with no failure signatures and no errored steps is a 5.** Do not hunt for something to deduct.

Evaluate the harness against the following criteria.

{criteria}
