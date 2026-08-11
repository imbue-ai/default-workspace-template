Reverted main to `c043d0f7a` ("Merge pull request #387 from imbue-ai/hynek/expect-auto-response"), the last commit that reached main through a real PR.

Commit `55d1e6e84` merged origin/main into a long-running agent working branch and pushed the result to main, landing roughly 90 commits of in-progress work that was not ready to ship. This revert takes that work back off main; the commits remain reachable and can be re-landed through their own PRs.

Removed from the workspace template as part of the revert: the `geopolitical_dashboard` app (a mock), the codex/pi policy-hook guards and the `POLICY_HOOKS.md` note, the claude statusline model-state recorder, the codex/pi launcher wiring in `setup_system.sh` and `supervisord.conf`, and the `harness-audit-2026-08-10`, `queue_sweep`, `shoulder_tap_atomic`, `agent-liveness-overlay`, and `live-model-state` design documents.

Kept intact: everything merged before that point, including PR #362 (browser streaming and fixes) and the `system/vendor/mngr` refresh at `029126212`, which still pins the vendored mngr to `22cd2f1839`.
