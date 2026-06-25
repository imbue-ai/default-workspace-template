Added the **Caretaker**: a nightly maintenance agent that quietly looks after your workspace.

Once a night the Caretaker can check the apps and services running here for problems -- a page that stopped loading, a service that crashed, errors piling up -- and either fix them or explain what it found, always in plain, non-technical language.

It introduces itself the first time it appears (as its own chat), and asks before it starts looking through your apps or making changes, so it never spends your time or budget by surprise. You stay in full control: you can change when it runs, ask it to take on other regular chores, or switch it off entirely.

Adds the `caretaker` agent template, the `caretaker` skill (its nightly routine), and the `run_caretaker.sh` wake script the scheduler invokes.
