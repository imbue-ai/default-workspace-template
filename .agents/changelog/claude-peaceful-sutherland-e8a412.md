- `publish-inspiration` gained a post-publish validation step (step 3b): after
  pushing and tagging a PUBLIC inspiration repo, the skill calls the minds
  API's new `GET /api/v1/inspirations/validate` endpoint (through the latchkey
  gateway's `minds-api-proxy`; granted to every workspace by default) to check
  the published repo against the inspiration contract -- bootable-template
  markers, the `minds-inspiration` topic, manifest front-matter, thumbnail
  gates, and leftover FILL-IN blocks. A 422 report means something slipped
  through the pre-push gates; the skill fixes the files in the assembly
  worktree and force-pushes a fresh snapshot commit before declaring the
  publish done. Private repos skip validation (the validator reads GitHub
  anonymously and cannot see them).
