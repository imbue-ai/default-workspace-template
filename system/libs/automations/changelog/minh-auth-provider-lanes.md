An automation binds the workspace's default provider account when it creates its agent. Cron has
no agent to inherit a credential from, so without this an automation -- including the weekly
Caretaker -- launched against a config directory holding no credential and could not take a
turn. Nothing signed in means no arguments and the old behaviour exactly.
