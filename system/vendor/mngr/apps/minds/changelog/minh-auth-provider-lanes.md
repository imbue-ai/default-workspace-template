Fix `test_litellm_via_workspace` for the workspace's account-based sign-in.

`/api/claude-auth/submit-credentials` now mints a provider account instead of overwriting
the workspace's shared claude login, and an agent binds to an account when it is CREATED
(the credential rides `mngr create`'s own flags). So the boot chat the test used to reuse
is still running on whatever it was created with, and never sees the minted key.

The test now creates a chat on the returned `account_id` and waits for mngr to register it
before messaging. It also asserts the submit response carries that id. `auth_mode` is
unchanged and still asserted.

Add `test_every_paste_lane_binds_a_chat_to_its_own_account`, and assert the binding in the
existing litellm test. Between them they are the workspace's sign-in artifact: one lane proven
end to end including a real turn and a spend row, and every lane that can be driven without a
browser proven through the part they share -- account minted, credential written where the
harness looks, chat created against it, and the agent actually pointed at that folder.

That last step is the one that fails silently. A chat bound to nothing is indistinguishable
from a working one until its first turn, and for codex a credential that lands in an OS keyring
rather than the account leaves a dangling symlink while `codex login status` still reports
success. The check resolves the binding the way each harness does: CLAUDE_CONFIG_DIR out of the
agent's env file for claude, a credential symlink under the agent's state directory otherwise.

The keys are deliberately fake. What is being tested is the join, not the provider.
