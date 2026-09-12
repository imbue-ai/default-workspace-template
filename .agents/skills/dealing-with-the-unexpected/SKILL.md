---
name: dealing-with-the-unexpected
description: Work out what is actually happening when the workspace does not behave as documented -- an unexplained error, state that makes no sense, or a command whose result contradicts these instructions. Walks through gathering evidence from tmux, supervisord and on-disk state before diagnosing, so you act on what is true rather than on what should be true. Use when you are surprised, not when a command simply failed for a reason you already understand.
metadata:
  author: imbue
---

# Dealing with the unexpected

When something unexpected happens, follow this procedure:

## 1. Gather information

Before doing anything, understand what is actually happening:

- Read any error messages carefully
- Check the tmux windows: `tmux list-windows -t $(tmux display-message -p '#S')`
- Check if services are running: `supervisorctl status` (compare against the programs defined in `system/supervisord.conf`)
- Check the wait counter state: `cat data/.state/wait_counter 2>/dev/null || echo "no counter"`

## 2. Diagnose

Common issues and their causes:

- **Services not starting**: Run `supervisorctl status` and read the service's logs under `/var/log/supervisor/<name>-stderr.log` (or `supervisorctl tail -f <name> stderr`). The `bootstrap` tmux window shows supervisord's own output. Verify `system/supervisord.conf` is valid, then `supervisorctl reread && supervisorctl update`.
- **Wait script behaving oddly**: Check `data/.state/wait_counter` contents. Delete it to reset: `rm -f data/.state/wait_counter`

## 3. Fix or escalate

If you can fix the issue yourself (edit a config, restart a service, etc.), do so and commit the fix.

If you cannot fix the issue, tell the user:
- What you observed
- What you tried
- What you think the problem might be
- Ask for their help

## 4. Never panic

You are a persistent agent. Even if something is broken, you will keep running. Focus on what you can do, communicate clearly with the user, and wait for help if needed.
