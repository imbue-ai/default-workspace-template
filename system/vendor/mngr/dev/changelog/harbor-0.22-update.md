The `specs/minds-eval-harbor/` record matches the stack the harness actually runs on: harbor
`v0.22.0` and `harbor-rewardkit==0.2.0`, in both the dependency pins it quotes and the `uvx`
invocation in the verifier's `test.sh`.

Two rewardkit claims the specs carried are corrected while they are open. Gate composition still
cannot live in a `reward.toml`, but the reason is that every gate aggregation collapses its group to
0.0/1.0 and discards the mean, not that programmatic `.py` criteria cannot carry `[scoring]` (at
0.2.0 they can). And only the *immediate* subdirectories of `tests/` are scoring dimensions --
rewardkit recurses below them, and a nested directory joins its parent dimension's weighted mean.
