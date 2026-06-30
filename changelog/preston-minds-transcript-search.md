- Added a `find-past-transcripts` skill so an agent can recover the chat history
  of past agents the user refers to -- including agents that were destroyed. It
  lists preserved transcripts via the Minds API (`GET /api/v1/workspaces/preserved`)
  and reads any agent's transcript (`GET /api/v1/workspaces/<id>/transcript`),
  reusing the `minds-api` skill's gateway/permission flow (`minds-workspaces-read`).

- Documented the two new transcript endpoints in the `minds-api` skill's read
  section, and added a "Finding past work" note to `CLAUDE.md` so agents know by
  default that earlier agents' chat history is preserved and retrievable.
