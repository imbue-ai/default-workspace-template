Before a template is published, the agent now shows you its README and asks
whether it describes what you built. It pastes the text into the chat, which
renders markdown, so you review a page rather than a wall of source, and if the
answer is no it rewrites and shows you again until you are happy.

The relative hero image does not resolve in a chat message, so the agent says
so rather than letting you read it as a broken README. The image itself is the
thumbnail you already approved, and the flow verifies the live GitHub page
after pushing, which is what actually catches a bad image path.

Repoints the broken `docs/system/style_guide.md` symlink. It targeted
`vendor/mngr/style_guide.md`, which resolves relative to `docs/system/` and so
pointed at a path left stale by the `system/` relayout; it now resolves to the
real file.

The repo-wide prose ratchets skip symlinks resolving into `system/vendor/`,
which is what they always intended -- until now that only worked because this
link was broken.
