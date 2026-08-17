Reverts the template licensing change (#407).

Publishing no longer asks how you want the template licensed, writes no
`LICENSE` file, and the generated README has no License section. Adopting a
template no longer reports what the repo's license permits, and the welcome a
published template ships no longer mentions it on first boot.

This restores the previous behaviour exactly: the publish flow mentions MIT in
passing when you choose public visibility, and nothing else about licensing
happens anywhere.

Worth knowing what goes back to being true. A published template carries no
license, so by default nobody who receives it has permission to use, copy, or
modify it -- being able to clone a repo is not a grant. Adopters are not told
that, and the flow does not ask the publisher what they intended.

The assembly script's tests are kept, minus the license-specific ones. Every
assertion in that file was about license behaviour, but the harness around them
was not: it builds a real workspace, adds a linked worktree, and runs
`build_template.sh` end to end, and it is the only thing in the repo that
executes that script. It now asserts what assembly does regardless of
licensing -- that it REFUSES to run outside a throwaway worktree (the guard
that exists because this once wiped a live workspace), that the manifest trio
is written, that the README and `/welcome` are regenerated to describe this
template, and that the source workspace's version history never ships.
