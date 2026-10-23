- `workspace_layout.identity` (moved from the shell) parses the `X-Imbue-Identity` header into a `RequestIdentity` and resolves it to a `ParticipantKey` (`owner`, `user:<id>`, `agent:<id>`); `workspace_layout.roster` carries the roster document (`RosterParticipant`, `RosterDocument`), the mention handles, and the participant colour palette.

- The shell client gains `roster()` for `GET /api/roster`, and the client inventory's records name each client's participant.
