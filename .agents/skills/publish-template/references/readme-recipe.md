# Writing a template's README

The published repo's `README.md` is its landing page -- the first thing a person
sees on github.com and what convinces them to boot it. `build_template.sh`
generates the structure; this is what goes in the FILL-IN blocks.

## The order, and why

1. **A hero graphic.** Already wired up: the README embeds `template.svg`,
   the bespoke thumbnail designed during assembly. A hand-authored SVG scales
   best, which is what that file already is.
2. **The "Open in Minds" call-to-action.** Generated, not written by hand. It is
   deliberately prose-free -- a large centered button plus a one-line copyable
   fallback beneath it, nothing else. The button points at the HTTPS trampoline
   (`https://boweiliu.github.io/open-in-minds/?git_url=...`), NEVER a bare
   `minds://` link, which GitHub renders dead. Both the button and the fallback
   carry the `MINDS_TEMPLATE_REPO_URL` placeholder until the lead substitutes
   the real owner/repo in §7; do not fill it in yourself and do not remove it.
3. **Why you care.** One or two plain sentences on the problem it solves. Not
   how it is built -- why someone would want it.
4. **How to use it.** How someone actually uses the thing once it is running:
   the commands, endpoints, screens, or workflow it exposes. This is the heart
   of the page, so give it the room it needs -- but default to concise and
   readable. A short list or a couple of worked examples beats a wall of prose.
5. **Ideas for making it yours.** Three to five concrete, specific changes
   someone could make after adopting it: "point it at a different channel",
   "swap the daily digest for a weekly summary", "add a second source alongside
   Slack". This is the section that turns a reader into an adopter, because it
   shows the thing is a starting point rather than a finished artefact.
6. **Anything else the app needs.** Screenshots with captions, config, security
   notes, architecture -- as much or as little as it warrants.

## Ideas are not Requirements

The manifest's `Requirements` section is the must-decide list: what is stubbed
or hardcoded and *has* to be resolved before the thing is really the adopter's.
Ideas are optional invitations -- things that already work fine but could be
taken somewhere else.

Keep them distinct and never repeat an item across the two. A reader who cannot
tell "you must fix this" from "you could try this" reads the whole page as a
list of defects.

## Show the user the rendered page, never raw markdown

The README is a page, so the user reviews it as one. This happens at the
publish flow's §6 confirmation gate and is driven by the LEAD -- never by the
assembly worker, whose worktree is background work and whose output the user
has not asked to see yet.

**Render it before you send the confirmation message**, so the page is already
on screen when they read your question:

```bash
uv run --project /home/user/workspace python \
    /home/user/workspace/system/scripts/render_markdown_preview.py "$WT/README.md"
python3 /home/user/workspace/system/scripts/layout.py open service:markdown-preview --layout <layout>
```

Two details that are load-bearing:

- **`--project /home/user/workspace`** keeps this compatible with the skill's
  CWD invariant. cwd stays `$WT`, which has no virtualenv at all -- the
  assembly's `git clean -fdxq` removed it -- while the interpreter and the
  preview service come from the workspace, which is where the supervisord
  program actually lives.
- **Pass `$WT/README.md` as an absolute path.** The server serves the previewed
  file's own directory, so this is what makes the relative hero image resolve.
  Render a copy from somewhere else and you get a broken image that is your
  fault, not the README's.

Then ask whether it reads like a good description of what they built. Name what
you want judged: whether "Why you care" frames the problem the way they would,
whether "How to use it" matches how they actually use the thing, and whether
the "Ideas for making it yours" are ones they would want an adopter to try.

**If they say no, rewrite and show them again** -- that is the entire point of
asking. Edit `$WT/README.md`, re-render with the same command, and refresh:

```bash
python3 /home/user/workspace/system/scripts/layout.py refresh service:markdown-preview --layout <layout>
```

Keep the generated structure. Their objection is almost always about the WORDS,
not the shape, and the hero, the Open in Minds call-to-action, and its
`MINDS_TEMPLATE_REPO_URL` placeholder must all survive any rewrite -- the
lead substitutes that placeholder in §7 and §8 blocks the push if it is
missing. Loop until they are happy.

**Close it when the review is over**, so they are not left with a panel they
did not ask for:

```bash
uv run --project /home/user/workspace python \
    /home/user/workspace/system/scripts/render_markdown_preview.py --close
```

That stops the service, which withdraws its port and takes the tab with it.
(The same reason the render command above is what starts it: the service is
never autostarted, so nothing appears until there is something to look at.)

## Verify the published page

The local preview only approximates GitHub; the published page is the real
end-to-end test. After the push, open the repo's README in the embedded
Chromium (drive it with the `agentic-browser-fleet` skill) and confirm:

- the hero graphic and any screenshots render, with no broken images. This is
  the real check: a relative path that is correct in the source tree can still
  404 on github.com, and only the published page shows it;
- the **Open in Minds** badge loads and its link goes to the trampoline.
  Clicking it opens the trampoline page; the final `minds://` hop only completes
  on a machine with Minds installed, so do not treat that as a failure;
- the copyable ` /use-template` line names the right repo (its leading space is
  deliberate -- a pasted `/...` can be read as a slash command), and every other
  link resolves.

Fix and re-push until the live page is clean.
