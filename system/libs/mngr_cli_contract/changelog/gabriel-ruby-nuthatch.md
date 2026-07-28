- `assert_mngr_argv_valid` now also checks every `-S KEY=VALUE` config override
  an argv carries, by running it through mngr's own `apply_settings_to_config`.
  click treats a `-S` value as an opaque string, so before this an argv could
  carry a key path that mngr rejects at startup -- taking the whole command down
  -- and still pass the contract check. The chat-create paths' fast-mode
  override is the first repo invocation to rely on this.
