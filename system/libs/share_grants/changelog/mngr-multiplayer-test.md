New library: the parsers for the workspace's two grants documents, the people's (`data/.secrets/share_grants.toml`) and the visiting agents' (`data/.secrets/agent_access/grants.toml`, with the gateway's last-seen record beside it), shared by the share gateway and the shell so each reads the same files one way.

Tidied the granted-app tests `grants_test.py` took over from the shell (the extra blank line before `test_render_grants_roundtrips_through_parse` is gone), and the README now names the granted-app query (`Grants.granted_app_names`) among the `grants` module's API.
