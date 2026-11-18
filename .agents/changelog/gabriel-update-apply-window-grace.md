An update no longer starts up and messages chats it paused only to free memory while they sat idle. `run-status stop-chats --agent <name>` records whether each named chat or background agent was busy (mid-turn, or waiting on a background command) when paused; `resume-chats` brings back and tells only the busy ones, and leaves an idle one stopped and untold, so the memory its pause freed stays free and no chat gets a "carry on" note with nothing to carry on. The next message to it starts it. A run record written before this change reads every paused agent as busy, as before.

The update's lead now stops its worker right before the apply (SKILL.md 5b): the worker has reported `done`, and its idle session held memory the apply's pre-flight boots needed. A retry or catch-up message starts it again with its branch and conversation intact.

When the memory guard stops one of the apply's pre-flight boots, the apply's failure line now says so, with when and how much the boot held, and that memory must be freed before a retry, instead of only "(the pre-flight boot wrote nothing at all)". It reads the guard's own kill record for the boot's pid.

The update memory guide tells the lead to judge free memory by the room the memory listing now reports above the guard's line.
