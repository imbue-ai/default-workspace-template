Fix `test_litellm_via_workspace` for the workspace's account-based sign-in.

`/api/claude-auth/submit-credentials` now mints a provider account instead of overwriting
the workspace's shared claude login, and an agent binds to an account when it is CREATED
(the credential rides `mngr create`'s own flags). So the boot chat the test used to reuse
is still running on whatever it was created with, and never sees the minted key.

The test now creates a chat on the returned `account_id` and waits for mngr to register it
before messaging. It also asserts the submit response carries that id. `auth_mode` is
unchanged and still asserted.
