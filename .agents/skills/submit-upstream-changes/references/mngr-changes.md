# Submitting mngr changes

mngr runs in this workspace as Python packages installed from the public mngr repo
at the commit `pyproject.toml` pins (`[tool.uv.sources]`, `imbue-mngr`). The
installed files under the tool's `site-packages` are a build of that commit, and the
next reinstall (any `mngr plugin add`, the update-self refresh, `uv sync`) puts that
build back, so there is nothing here to edit or commit.

**The rule: mngr changes are not template changes. They are developed and tested in
an mngr-internal checkout on a developer's machine, land as their own mngr PR, and
reach this template by a pin bump once the public mirror carries them.** Until then a
template change that needs them cannot be verified here, since this template's CI
only ever builds against the pin.

What an agent in this workspace does with a needed mngr change: describe it (what,
where in mngr, why the template needs it) in the workspace's own PR or ticket, and
build the template side against the current pin. The pin bump follows the mngr PR:
in the mngr repo, `just dwt-mngr-pin` resolves a merged mngr commit to its
public-mirror commit and rewrites every `rev` under `[tool.uv.sources]` here (all of
them carry the same commit; `uv` refuses to mix commits across packages from one
repo), relocks, and commits. A developer with a checkout of both repos can also
pin a template branch to an unmerged mngr commit on the private mngr-internal repo
(`just dwt-mngr-pin-internal`) to build the pair before the mngr side lands; that
needs a credential at build time, and before the template branch merges the pin
moves to a public export of the mngr branch (`just dwt-mngr-pin-export`), since
`main` never carries a private pin. None of that is something an agent in a
workspace does.

A defect found in mngr is different from a change the template's own work needs:
it goes in the report POST in `.agents/shared/references/report-built-in-issues.md`,
per AGENTS.md's rule on built-in defects, since a ticket here never reaches mngr.
