# Reuse what already exists

- `AGENTS.md` adds two rules. When a creation already does what is being asked,
  run it or extend it and run it, rather than producing the same result by hand
  beside it. And a change to code a harden pass already covered extends that
  code's tests in the same commit.

- The `modal_eval` create template gives an eval workspace 4h10m instead of 3h. The
  timeout is only the backstop for a driver that dies without tearing its
  workspaces down, but because every step of a stepped case shares one workspace
  it also caps how long those steps may run together -- and two roadmap trials
  were lost to a step hitting its share of that cap rather than to anything the
  agent did. Raising it costs a longer leak in the one failure case it guards.
