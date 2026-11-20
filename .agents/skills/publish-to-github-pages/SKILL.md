---
name: publish-to-github-pages
description: Publish one of the user's apps as a public static website on GitHub Pages, on their own GitHub account. Checks first whether the app can run as a static site (pages and browser-side code only, no server), rewrites a copy of it into one when that is possible, and refuses with a plain reason when it is not. Walks a user with no GitHub account through making one, and takes a published site down on request. Use when the user asks to publish, host, or put an app on the web / on GitHub Pages / at a public link that works while the workspace is off.
compatibility: Requires latchkey (see the connect-external-service skill) and the user approving GitHub permissions in the Imbue Studio app.
metadata:
  author: imbue
---

# Publish an app to GitHub Pages

GitHub Pages serves files -- HTML, CSS, JavaScript, images, data files -- from a
GitHub repo at `https://<owner>.github.io/<repo>/`. It runs no code of its own.
Every app in this workspace is a small server program, so what gets published is
never the app itself: it is a **static copy** of it, built in a folder of its own,
that does everything in the visitor's browser.

## Hard rules

- **The live app is never changed.** All rewriting happens in the published copy.
  If the user wants the app itself changed, that is the `update-app` skill's job,
  not this one.
- **Everything published is public, forever.** The repo is public (Pages on a
  private repo needs a paid GitHub plan, and the site is public either way), and
  anything pushed can be copied before it is deleted. Never publish personal data,
  secrets, API keys, tokens, or anything read from the user's connected accounts
  unless the user explicitly confirmed that exact data at the publish gate.
- **One app per repo, never the workspace's own repo.** Each published app gets a
  new dedicated public repo. Never enable Pages on the GitHub sync repo or the
  upstream template repo.
- **Everything flows through latchkey.** Never ask the user for a GitHub token.
- **Ask only about decisions with significant consequences.** Everything else
  (the repo name, which pages to copy, how to rewrite) you decide and mention at
  most in passing. Two moments can need the user, and each needs an explicit reply
  to that message; end your turn after sending one:
  - the **trade-offs** (step 2), only when the published site will behave
    noticeably differently from the app or carry data;
  - the **publish gate** (step 5), always on a first publish, because it makes
    something public. On a republish, only when it adds data or new trade-offs.

## Values carried through the flow

- `app`: the app's registered name (the `name` in its `app.toml`; `uv run
  --no-sync workspace-layout list` lists every app).
- `repo`: the repo name. Defaults to `app`.
- `owner`: the user's GitHub username. Taken from the connected account when
  there is one (step 0), asked for only when there is not.
- `SITE`: the static copy's folder,
  `data/.skills/publish-to-github-pages/sites/<app>/`. It is kept after
  publishing, so a later republish starts from it.
- `LEDGER`: `data/.skills/publish-to-github-pages/sites.json`, one entry per
  published app, read and written only through `ledger.py`.
- `P=.agents/skills/publish-to-github-pages/python/publish_to_github_pages_skill`: this skill's tools. Run
  every command from the repo root. Each tool's docstring has its full usage.

Every command runs in a fresh shell, so set the values it uses at its start
(`P=...; SITE=...; uv run --no-sync "$P/..."`).

`snapshot_app.py` copies an app into `SITE`, `check_site.py` prepares, lints and
browser-checks it, `github_pages.py` does everything on GitHub, and `ledger.py`
keeps the record.

## Steps

```
- [ ] 0. A GitHub account (guide the user to make one if needed)
- [ ] 1. Pick the app and inspect it
- [ ] 2. Fit verdict                              -- end turn, wait (only if trade-offs)
- [ ] 3. Build the static copy
- [ ] 4. Check the copy locally
- [ ] 5. Publish gate                             -- end turn, wait (first publish)
- [ ] 6. GitHub access
- [ ] 7. Create the repo and push
- [ ] 8. Turn on Pages and check the live site
- [ ] 9. Record it and tell the user
```

In the user's progress view these are a few plain steps ("Check whether your app
can be published", "Make a web-ready copy", "Publish it"), not this list.

Start every run with `uv run --no-sync $P/ledger.py get --app <app>`. If it prints an
entry, this is a **republish**: skip the account guide, reuse `owner`/`repo`,
rebuild `SITE` from the current app, and push to the same repo (steps 3-5, 7-9).

## 0. A GitHub account

First run `uv run --no-sync $P/github_pages.py whoami`. If it prints a username, GitHub is
already connected: that is `owner`, and the rest of this step is skipped.
Otherwise ask:

> Publishing puts your app on GitHub, a site that can host your web page for free.
> Do you have a GitHub account?

- **Yes:** file the GitHub permission requests now (step 6), so approval waits
  in Imbue Studio while you work. The approval message names the account
  ("granted for account '<username>'"), and that is `owner`. Ask the user for
  their username only if neither gives one, and say why: it is part of the
  site's address.
- **No:** send this guide, then wait for them to say they are done:

  > Here's how to make one (free, about two minutes):
  >
  > 1. Go to <https://github.com/signup>.
  > 2. Enter your email, pick a password, and choose a username. Your username
  >    becomes part of your site's address (`username.github.io`), so pick one
  >    you're happy to share.
  > 3. Enter the code GitHub emails you.
  > 4. Skip any questions about plans or teams -- the free plan is all you need.
  >
  > Tell me when you're signed in, and I'll connect it.

  When they reply, file the permission requests (step 6) and take `owner` from
  the approval message. Approving them is where Imbue Studio asks the user to
  sign in to GitHub, so they do not need to do anything else on GitHub
  themselves.

## 1. Pick the app and inspect it

Ask which app only if the request leaves it unclear. Read it to answer one
question: **what does it need a
server for?** Look at:

- its routes (the code its `app.toml` and its supervisord program point at): which
  return pages, which return data, which change data;
- where its data lives (`data/.apps/<app>/` and anything else it reads or writes);
- whether it calls an AI model, a connected account (latchkey), another app, or an
  outside site;
- whether it is driven by a routine or does work in the background.

## 2. Fit verdict

Sort the app into one of three. Tell the user only what a visitor would notice:
a rewrite that leaves the app working the same (calculations, sorting or search
moved into the page) is done silently, never listed.

- **Static already.** Its pages work with no server at all, or its server only
  hands out pages and fixed files. Do not stop to ask: go straight to step 3, and
  mention any visible difference (for example, "it starts empty for every
  visitor") at the publish gate.
- **Can be rewritten.** Its server work can move into the browser or be frozen at
  publish time. Name only the changes a visitor would notice, each with what they
  lose:
  - data it shows that changes rarely: **frozen into the page** as a snapshot; the
    site only updates when it is republished (a routine can republish on a
    schedule -- offer it, do not set it up here);
  - data a visitor creates (notes, settings, scores): **saved in the visitor's own
    browser** (local storage), so it is private to them and not shared;
  - calls to an outside site: only if that site allows browser requests from other
    sites (CORS) and needs no key; otherwise frozen as a snapshot or dropped.

- **Can't be published here.** Any of these is a hard no, with the reason in one
  sentence:
  - it needs a secret or a login (an AI model, a connected account, any API key);
  - several people must see each other's saved data;
  - it must act on its own (send messages, sync, watch something);
  - it controls something in the workspace (other apps, files, the desktop).

  Say so and stop; there is nothing for the user to decide. Offer the
  alternative: the workspace's own sharing link (the `use-from-phone` skill covers
  turning it on) keeps the app running with its data, but only while the
  workspace is on.

Only for **Can be rewritten** with a change a visitor would notice, or a data
snapshot: end your turn and wait for the user to agree to the trade-offs. With
neither, carry on to step 3 as for **Static already**.

## 3. Build the static copy

Build into `SITE`. Never edit anything the live app runs from.

1. **Copy the app.** With the app running (`supervisorctl start <program>` if it
   is stopped):

   ```bash
   uv run --no-sync $P/snapshot_app.py --app <app> --site $SITE --fresh \
       --page / [--page /other-page ...] [--data /api/<route>=data/<name>.json ...]
   ```

   Pass every page a visitor can reach, and one `--data` per frozen snapshot. An
   app whose page is built by a frontend bundle can instead have its build output
   copied into `SITE`.
2. **Strip the data.** Open each saved data file and remove anything the user did
   not agree to publish.
3. **Rewrite.** Point each remaining server call at its data file or replace it
   with page code or local storage, as the verdict said. A `fetch()` path is
   relative to the **page**, not the script (`fetch("data/items.json")` from the
   home page). Keep the visible page the same.
4. **Add the GitHub Pages files**:

   ```bash
   uv run --no-sync $P/check_site.py prepare --site $SITE --title "<title>" --description "<one line>" [--spa]
   ```

   `--spa` for an app with several views on one page, so deep links load it.
5. **Lint** until clean:

   ```bash
   uv run --no-sync $P/check_site.py lint --site $SITE
   ```

   Fix each finding. Only a finding the user explicitly confirmed may ship (an
   email they want public, a library's harmless mention of `localhost`) goes in
   `SITE/.publish-allow` as `path:line`. Never allow-list a secret.

## 4. Check the copy locally

```bash
uv run --no-sync $P/check_site.py browse --site $SITE --repo <repo> \
    --page / [--page other-page/ ...] --screenshot-dir /tmp/<app>-site
```

A clean run only means nothing failed to load. Then try the app's main actions
yourself as a visitor would, including anything saved in the browser (reload and
check it is still there), and compare the screenshots with the live app.

Fix and re-check until it passes. Nothing goes to GitHub until it does.

## 5. Publish gate

The gate needs the real address, so `owner` must be known first. If the GitHub
approvals have not arrived yet, wait for them; if the user named a username,
check it matches the connected account (step 6).

Send ONE message, in plain language:

- the address it will have, written out in full with their real username:
  `https://<owner>.github.io/<repo>/`. Never a placeholder like
  "your-username.github.io";
- that the site and its files are **public**, and what data goes with it (each
  snapshot, in a few words), or "no data";
- what a visitor will notice works differently from the app in the workspace
  (from the verdict), or "works the same";
- the main screenshot from step 4, shown inline.

Then end your turn and wait for an explicit yes. A republish that adds no data and
no new trade-offs skips this gate and goes straight to step 7.

## 6. GitHub access

Request the same permissions as the `github-sync` skill, in two tool calls of
their own, back to back. Its step 2 has the exact commands; use this skill's
rationale instead, one short plain sentence per request (it is the text on the
user's approval card):

- `github-git` scope: `github-git-read`, `github-git-write` -- push the site.
- `github-rest-api` scope: `github-read-user`, `github-read-repos`,
  `github-write-all` -- read the account name, create the repo (only
  `github-write-all` covers repo creation), and turn on Pages.

Wait for both approval messages. Then read the connected account's username
with `uv run --no-sync $P/github_pages.py whoami`. If the user named a username and it
differs from this one, the address at the publish gate was wrong: tell them which account
is connected, and ask whether to publish there (and re-confirm the address) or
connect the other account. The same
"request not permitted" caveats as `github-sync` apply: a rejected endpoint is not
a missing grant.

## 7. Create the repo and push

On a first publish, create the **public** repo:

```bash
uv run --no-sync $P/github_pages.py create-repo --name <repo> --description "<one line>" --owner <owner>
```

Exit 3: the name is taken -- ask the user for another
rather than numbering it silently, since the name is part of the address. Exit 4:
the connected account is not `<owner>` -- go back to step 6's mismatch handling.

Then publish the files (first publish and republish alike):

```bash
uv run --no-sync $P/github_pages.py push --site $SITE --repo <owner>/<repo> --message "Publish <title>"
```

If it refuses, fix what it names; never work around it (no force-push, no
pushing from another folder).

## 8. Turn on Pages and check the live site

```bash
uv run --no-sync $P/github_pages.py enable --repo <owner>/<repo>
uv run --no-sync $P/github_pages.py wait --repo <owner>/<repo>
uv run --no-sync $P/check_site.py browse --live-url https://<owner>.github.io/<repo>/ \
    --page / [--page ...] --screenshot-dir /tmp/<app>-live
```

If `wait` reports a failed build, fix its reason and push again. A first publish
can take a few more minutes to answer at its address after the build is done: if
`browse` gets a 404, wait a minute and retry.

## 9. Record it and tell the user

```bash
uv run --no-sync $P/ledger.py record --app <app> --owner <owner> --repo <repo> \
    --url https://<owner>.github.io/<repo>/ --published-commit <commit from push> \
    --data "<the snapshots, in a few words, or none>"
```

Then tell the user:

- the live link;
- in one line, what a visitor will notice works differently, if anything;
- that changes to the app do not appear on the site until they ask to republish.

## Taking a site down

If the user asks to remove a published site, follow `references/takedown.md`.
