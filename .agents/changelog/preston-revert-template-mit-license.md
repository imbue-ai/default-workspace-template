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
