# Writing an inspiration's README

The published repo's `README.md` is its landing page -- the first thing a person
sees on github.com and what convinces them to boot it. `build_inspiration.sh`
generates the structure; this is what goes in the FILL-IN blocks.

## The order, and why

1. **A hero graphic.** Already wired up: the README embeds `inspiration.svg`,
   the bespoke thumbnail designed during assembly. A hand-authored SVG scales
   best, which is what that file already is.
2. **The "Open in Minds" call-to-action.** Generated, not written by hand. It is
   deliberately prose-free -- a large centered button plus a one-line copyable
   fallback beneath it, nothing else. The button points at the HTTPS trampoline
   (`https://boweiliu.github.io/open-in-minds/?git_url=...`), NEVER a bare
   `minds://` link, which GitHub renders dead. Both the button and the fallback
   carry the `MINDS_INSPIRATION_REPO_URL` placeholder until the lead substitutes
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

## Verify the published page

The local file is not the deliverable; the rendered GitHub page is. After the
push, open the repo's README in the embedded Chromium (drive it with the
`agentic-browser-fleet` skill) and confirm:

- the hero graphic and any screenshots render, with no broken images. This is
  the real check: a relative path that is correct in the source tree can still
  404 on github.com, and only the published page shows it;
- the **Open in Minds** badge loads and its link goes to the trampoline.
  Clicking it opens the trampoline page; the final `minds://` hop only completes
  on a machine with Minds installed, so do not treat that as a failure;
- the copyable `/use-inspiration` line names the right repo, and every other
  link resolves.

Fix and re-push until the live page is clean.
