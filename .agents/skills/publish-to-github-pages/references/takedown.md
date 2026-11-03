# Taking a published site down

Use when the user asks to remove, unpublish, or delete a site this skill published.
Run from the repo root with `P=.agents/skills/publish-to-github-pages/python/publish_to_github_pages_skill`; read
`owner` and `repo` from `uv run --no-sync $P/ledger.py get --app <app>`.

Confirm which they want, since deleting cannot be undone:

- **Turn the site off, keep the files** on GitHub:
  `uv run --no-sync $P/github_pages.py disable --repo <owner>/<repo>`.
- **Delete it entirely** (cannot be undone):
  `uv run --no-sync $P/github_pages.py delete-repo --repo <owner>/<repo> --yes-delete-forever`.

Then `uv run --no-sync $P/ledger.py remove --app <app>`. Tell them copies others made
before the takedown cannot be recalled.
