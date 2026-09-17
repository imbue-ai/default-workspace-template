# PyPI trusted-publisher checklist

Publishing runs from `imbue-ai/mngr-internal`. A trusted publisher is identified
by owner + repo + workflow filename + environment, so every published project
needs an entry for this repo. There is no PyPI management API for this: it is
manual web-UI work, one project at a time.

For each project below, at
`https://pypi.org/manage/project/<project>/settings/publishing/`:

1. Add a GitHub publisher: owner `imbue-ai`, repository `mngr-internal`,
   workflow `publish.yml`, environment `pypi`.
2. Remove any `imbue-ai/mngr` publisher, so the public mirror cannot publish.

A project that has never been published has no settings page yet: add a
PENDING publisher at `https://pypi.org/manage/account/publishing/` with the
same fields plus the project name, before the release that first publishes it
(`scripts/release.py` prompts for this when it detects a new package).

PyPI project names (the publishable set: every `libs/*` package except
`UNPUBLISHED_PACKAGES`; extracted from each package's `[project] name`):

- concurrency-group
- imbue-common
- imbue-mngr
- imbue-mngr-antigravity
- imbue-mngr-autocompact (never published: add a PENDING publisher)
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
- imbue-mngr-vps (never published: add a PENDING publisher)
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
