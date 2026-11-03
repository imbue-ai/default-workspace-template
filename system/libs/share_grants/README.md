# share_grants

The two grants documents of a shared workspace, as records with their parsers,
renderers, and the file locks every writer of them holds:

- `grants` -- `data/.secrets/share_grants.toml`, the people the owner shares
  the workspace (or one of its apps) with: user ids, invited emails, and email
  domains, per scope (`share_gateway/README.md`, "Grants"). `parse_grants`,
  `render_grants`, the invite upgrade that turns an admitted email into its
  user id, `locked_grants_file`, and the query for which apps the document
  shares on their own (`Grants.granted_app_names`).
- `agent_grants` -- `data/.secrets/agent_access/grants.toml` and
  `invites.toml`, the visiting agents admitted by a bearer token and the
  one-time codes they join with (`share_gateway/README.md`, "Visiting
  agents"). The records, their parsers and renderers, the invite codes and
  admission tokens, the `authorized_keys` reconcile, and the agent-access
  lock. The one check that needs a key library (that a joining agent's key is
  an Ed25519 OpenSSH key) is the gateway's own, in its `agent_keys` module.
- `json_values` -- the `is_json_object` guard both parsers read TOML tables
  with.

The share gateway writes these files and admits visitors by them; the shell
reads them for the workspace's roster (who may be here) and for which apps
are shared on their own. Both depend on this library rather than on each
other, so neither carries a copy of the other's parser. It imports nothing
outside the standard library, so a bare `python3` can use it too.
