The reveal steps that restart the services agent now rebuild the user's view of the workspace afterwards, via the new `system/scripts/refresh_workspace_view.py`.

`update-system-interface`'s reveal script previously told open browsers to reload only when the *frontend* had changed. A backend-only reveal restarted the services agent and refreshed nothing, leaving the open page rendering from what it had already fetched. The reload now runs after every restart, on both the reveal and the post-rollback recovery path, and goes through the shared helper so it also reaches the Minds app (which works when the page's WebSocket never came back from the restart).

`update-app` and `update-self` prescribed the same restart with no refresh at all; both now call the helper, and say why it is not optional -- the Minds app only intervenes when a workspace looks unreachable for a sustained stretch, which a quick restart never does.

`assist` now resolves the workspace's primary agent id by the `is_primary` label alone when filing a bug report. It also required a `workspace` label, which the Minds app stopped setting some time ago, so the lookup always came up empty and the report was filed under the calling agent's own id instead. The gateway accepts any agent id on the bug-report route, so this never surfaced as an error -- it just scoped the report to the wrong agent.
