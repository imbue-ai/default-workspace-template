# Stopping when asked

If the user tells you to stop or cancel the update, end the pass. Record the
verdict first, since until one is recorded the app restarts this chat whenever
it stops:

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    run-status verdict REFUSED --detail "Stopped at the user's request."
```

Then, if SKILL.md Step 3b launched the worker, stop it keeping its branch (`mngr stop
update-self`). Run SKILL.md Step 6's opening commands, release the `updating workspace` lease (`tk close
"$UPDATE_LEASE_ID" "Stopped at the user's request."`), and tell them nothing was
applied. Before SKILL.md Step 2 has staged the skill, record the verdict with
`.agents/skills/update-self/scripts/update_self.py` instead, and only release
the lease if you hold it: nothing has been launched, bridged or paused yet. An
apply already under way is never stopped halfway: it lands or rolls back on its
own, and the pass ends by that outcome instead.
