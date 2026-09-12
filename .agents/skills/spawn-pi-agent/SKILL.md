---
name: spawn-pi-agent
description: Spawn a mngr agent running the pi harness with a chosen provider/model, optionally monitor it, and post a formatted Slack thread reply when it finishes.
metadata:
  author: imbue
---

# spawn-pi-agent

Use this skill when you want to hand a task to a new `pi` harness agent and,
optionally, have a short, signed Slack reply posted back to a thread once the
agent finishes.

## Scripts

- `scripts/spawn_pi_agent.sh` — create the agent and optionally start a monitor.
- `scripts/monitor_and_reply.sh` — poll an agent's transcript and post a Slack
  reply when it looks done.
- `scripts/post_slack_reply.sh` — post a single Slack thread reply in the
  required format.

## Quick spawn

```bash
./.agents/skills/spawn-pi-agent/scripts/spawn_pi_agent.sh \
  --name my-task \
  --task-file ./task.md \
  --provider opencode-go \
  --model kimi-k2.7-code
```

## Spawn + auto Slack reply

```bash
./.agents/skills/spawn-pi-agent/scripts/spawn_pi_agent.sh \
  --name my-task \
  --task-file ./task.md \
  --provider opencode-go \
  --model kimi-k2.7-code \
  --slack-channel C02DKHNCUUV \
  --slack-thread-ts 1789158613.624079 \
  --monitor
```

## Slack reply format

The monitor builds replies that are:

- Short (body + evidence link <= 160 characters)
- Readable top-down without this conversation's context
- Signed with `sent from :brain:` and `[mind:<agent-id>@<hostname>]`

Example:

```
Fire alarm/inspection Mon 9/14 8am-12pm.
https://imbue-ai.slack.com/archives/C02DKHNCUUV/p1789159098211139
sent from :brain:
[mind:agent-a990e3a9bc6b4437bf385a7709834a56@fresh-052]
```

## Manual Slack reply

```bash
./.agents/skills/spawn-pi-agent/scripts/post_slack_reply.sh \
  --channel C02DKHNCUUV \
  --thread-ts 1789158613.624079 \
  --body "Fire alarm/inspection Mon 9/14 8am-12pm." \
  --link "https://imbue-ai.slack.com/archives/C02DKHNCUUV/p1789159098211139"
```

## Notes

- The spawned agent runs in its own git worktree by default (`mngr create`).
- `--provider` defaults to `opencode-go` and `--model` defaults to
  `kimi-k2.7-code`.
- The monitor runs in the background via `nohup` and logs to
  `/tmp/spawn-pi-agent-<name>.log`.
- The monitor posts once, then exits. It gives up after `--timeout` seconds
  (default 1 hour) and posts a brief status update if the agent did not
  clearly finish.