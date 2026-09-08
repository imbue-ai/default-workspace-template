# Browser skills: own with the fleet, drive with playwright-cli

`agentic-browser-fleet` no longer drives browsers -- it owns them. The skill now covers
`new` / `ls` / `close` / `acquire` / `release` / `handoff` and the human-takeover etiquette,
and `new` prints the `playwright-cli attach --cdp=...` line to drive with. The driving half
of the skill (the loop, element indices, the requery rule, the command table) is gone, along
with the `task` verb and its Anthropic API key requirement.

The skill keeps its name and stays the single entry point. It no longer restates the driving
commands -- `playwright-cli --help` ships with the pinned version and cannot drift -- and gains
the things `--help` cannot know about this workspace:

* the fleet owns lifecycle, so never `close` / `detach` / `close-all` / `kill-all`
* a session whose socket dropped is unrecoverable: it hangs rather than erroring
* `playwright-cli` exits 1 for everything, so on any failure run `ls` and branch on that
* a crash looks like a plain disconnect from the agent's side, and the dead name must not be
  re-attached
* **"not found" is not "not there"** on a virtualized list -- with a check that tells
  virtualized from infinite-scroll from ordinary-long, and the rule that you are at the bottom
  when rows stop advancing, not when `find` fails
* `mousewheel` fires at (0,0) unless you `hover` the pane you mean first
* snapshots are cheap and screenshots are not, with the narrow set of cases where a screenshot
  is the only thing that can answer the question

The fleet skill's idle-lease figure is corrected from 90s to 60s, which is what the code has
enforced for some time.
