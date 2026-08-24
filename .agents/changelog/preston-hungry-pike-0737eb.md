The `latchkey` skill now tells agents to post a permission request directly
instead of first asking the user in chat whether to ask -- the Minds app
already presents the request with Approve and Deny buttons, so the extra
check put the same decision in front of the user twice.

It also notes that an approved grant is written to the agent's permissions
file and enforced from then on, so a scope the user has already allowed will
not prompt them again. Agents should make the call first and only post a
request when it actually comes back blocked.
