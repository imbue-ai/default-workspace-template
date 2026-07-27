# PyPI trusted-publisher re-registration checklist

Publishing moves to `imbue-ai/mngr-internal` at cutover. A trusted publisher is
identified by owner + repo + workflow filename + environment, so every published
project needs a new entry. There is no PyPI management API for this: it is
manual web-UI work, one project at a time.

For each project below, at
`https://pypi.org/manage/project/<project>/settings/publishing/`:

1. Add a new GitHub publisher: owner `imbue-ai`, repository `mngr-internal`,
   workflow `publish.yml`, environment `pypi`.
2. Do NOT remove the existing `imbue-ai/mngr` publisher yet — both stay active
   until the first successful private-repo publish (add-before-remove, so a
   release never hits a gap).
3. Removing the `imbue-ai/mngr` entries afterwards is a cutover-day step (it is
   what makes the public mirror unable to publish).

PyPI project names (37 = the publishable set: every `libs/*` package except
`UNPUBLISHED_PACKAGES`; extracted from each package's `[project] name`):

- concurrency-group
- imbue-common
- imbue-mngr
- imbue-mngr-antigravity
- imbue-mngr-aws
- imbue-mngr-azure
- imbue-mngr-claude
- imbue-mngr-claude-usage
- imbue-mngr-codex
- imbue-mngr-codex-usage
- imbue-mngr-file
- imbue-mngr-forward
- imbue-mngr-gcp
- imbue-mngr-imbue-cloud
- imbue-mngr-kanpan
- imbue-mngr-latchkey
- imbue-mngr-lima
- imbue-mngr-modal
- imbue-mngr-notifications
- imbue-mngr-opencode
- imbue-mngr-opencode-usage
- imbue-mngr-ovh
- imbue-mngr-pair
- imbue-mngr-pi-coding
- imbue-mngr-pi-coding-usage
- imbue-mngr-recursive
- imbue-mngr-robinhood
- imbue-mngr-schedule
- imbue-mngr-ttyd
- imbue-mngr-tutor
- imbue-mngr-usage
- imbue-mngr-vps (never published: add a PENDING publisher at https://pypi.org/manage/account/publishing/ with the same fields plus this project name)
- imbue-mngr-vultr
- imbue-mngr-wait
- modal-proxy
- overlay (SKIP: the PyPI name `overlay` belongs to an unrelated third-party package; imbue's overlay lib has never been publishable under this name -- rename it or add it to UNPUBLISHED_PACKAGES before it can ever publish)
- resource-guards

Regenerate this list before clicking (it is a convenience snapshot, not the
source of truth):

```sh
for d in $(uv run --all-packages scripts/verify_publish.py --list-package-dirs); do
  grep -m1 '^name = ' "$d/pyproject.toml"
done
```
