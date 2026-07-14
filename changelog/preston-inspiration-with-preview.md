- The publish-inspiration flow now opens a PREVIEW of the inspiration in a
  new tab during the final chat confirmation. After assembly, the lead
  renders a static preview page from the assembled snapshot with the new
  `.agents/skills/publish-inspiration/scripts/render_preview.sh`, serves it
  with a background localhost `python3 -m http.server`, and registers it as
  the `inspiration-preview` tab via `scripts/forward_port.py`. The
  confirmation's hard-gate semantics are unchanged -- the preview is
  additive, and the thumbnail is still embedded in chat.

- The preview page carries a prominent full-width PREVIEW banner ("PREVIEW --
  nothing has been published yet; publishing happens only after you confirm
  in chat", repeated at the bottom) and a plain breakdown of (a) what is in
  the preview itself (rendered locally from the snapshot; mock-data
  thumbnail; nothing has left the machine), (b) what WILL be published (the
  selected app/feature paths, the generated manifest/thumbnail/README/welcome,
  carried-forward earlier inspirations, the template base's top-level tree,
  and the guarantee that the published git history is only the public
  template history plus one snapshot commit), and (c) what is NOT published
  (chats/transcripts/memory, secrets and .env files blocked by the
  two-scanner secret gate, unselected apps and files, the mind's git history,
  uploads, and any published-version modifications -- or "none requested").
  Every fact on the page is derived from the assembled worktree's actual
  contents (manifest front-matter and body, `git ls-tree`, the thumbnail);
  the page is a single self-contained HTML file with inline CSS and no
  external resources.

- The preview server and tab are torn down at every exit from the flow --
  the §10 close-out and every §6/§7/§8 abort path -- by killing the recorded
  server PID (never a pattern-based pkill) and removing the
  `inspiration-preview` registration; a re-publish removes any stale preview
  before registering anew. The skill's CWD invariant gains one documented
  exception for this plumbing: `forward_port.py` runs from `/code` because
  the tab registry is the live mind's gitignored `runtime/applications.toml`
  (no git operations, no tracked files touched).
