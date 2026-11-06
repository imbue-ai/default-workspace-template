# Resuming after `stuck`

When the user overrides a `stuck` verdict, resume the same pass with the same
worker as `.agents/shared/references/lead-proxy.md` ("Resuming after the user
overrides a failure") says. This flow's preconditions, re-taken before the
`reply`, each `tk` call as its own command:

1. The `updating workspace` lease, exactly as SKILL.md Step 1 takes it.
2. The run record, and the target the task file carries:

   ```bash
   python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
       run-status start
   REF=$(sed -n 's/^target_ref: //p' data/.tasks/update-self/task.md)
   ```

3. The history bridge, with SKILL.md Step 3a's two commands (the forced tag fetch,
   then `bridge-history --ref "$REF"`).

After the `reply`, `run-status delegate update-self` before the `await` poll,
as SKILL.md Step 3b does.
