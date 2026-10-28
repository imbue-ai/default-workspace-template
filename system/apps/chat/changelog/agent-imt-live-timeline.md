The chat shows one progress timeline at a time, always as the last thing in the conversation while any of its steps is still open, instead of a timeline under each user message.

- A user message or agent reply that arrives while steps are open renders above the timeline. A step open across several messages shows once, with all its work, and is no longer repeated under each new message.

- The timeline lists its steps by state: done (in the order they closed), the one being worked on now, the others in progress, then the ones not started. When every step is done the timeline stays where it is, and what follows (usually the agent's wrap-up reply) renders below it. The next step starts a new timeline.

- The step the chat makes for a message sent while the agent is busy shows on the live timeline as waiting about two seconds after sending, before the agent starts it. The line under the user's message for that step is gone.

- One small-model call now decides what such a message does to the chat's open steps, the agent's own and the ones made for messages, each listed with whether it is in progress or not started: a new step; the same as an open step; a change to one, which gets its own step for the updated request; or calling one off. The chat app writes only through tk: `tk create --id` for the message's own step (refused when a ticket already exists there) and `tk cancel` for a step called off or replaced. The timeline hides a step whose cancel holds, which lasts until the agent starts the step again. Same three attempts and 10 s limit per attempt as before. Only open steps of the same chat are offered to the model.
