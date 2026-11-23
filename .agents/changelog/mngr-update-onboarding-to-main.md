A new `first-run-onboarding` skill holds the agent's replies in a new workspace's first conversation: what to say when the user picks "Describe what you want to make" or "See what Studio can do" under the welcome (the second also opens the Getting Started app beside the chat). The chat app sends the first chat's agent to it, and any other first message is answered as an ordinary request.

The `first-run-onboarding` skill gains the path behind the welcome's new "Get ideas for what to make" suggestion: the two questions it asks (what the workspace is for, and which apps the user has, each with a picker tag the chat draws), the rules for the list of ideas that follows, and the replies to "More ideas", a picked idea, and "Something else". A new reference beside it, `references/app-ideas.md`, holds starter ideas for each category and what makes an idea a good fit for Studio.

The `build-app` skill's scaffolder sets the workspace's `app_build_started` onboarding marker on every run, so the chat does not suggest building an app while one is being built. A run that cannot write the marker prints a warning and scaffolds as before.

The `first-run-onboarding` skill lists Books, Movies and Music as three Creative categories in place of "Books, movies & music", and its ideas reference has a row of ideas for each. The skill also says that the pickers' "Other" pills put "Write whatever you want!" and "Write whatever data you want here!" in the message as prompts to write over, so either sentence arriving unchanged names no category and no app.

The `update-self` skill counts the Onboarding checklist's frontend among the bundles its apply builds, installs, snapshots and verifies (`--worker-bundle onboarding_checklist=<path>`), so a worker now reports eight built bundles.
