The nightly minds-evals workflow's `notify` job posts its Slack report with `minds-evals post-slack-report` instead of curling the webhook.

It fetches both `mngr/ci/SLACK_MINDS_EVALS_BOT_TOKEN` and `mngr/ci/SLACK_MINDS_EVALS_WEBHOOK` from Vault: the token posts as an app, which is what lets the report's replies thread under their message, and the webhook is the fallback. The channel id rides as a plain workflow value (`SLACK_CHANNEL_ID: C0BUFUVU0T0`), since an id names a channel and grants nothing.

The step summary carries each thread's message and, indented under it, each of its replies.
