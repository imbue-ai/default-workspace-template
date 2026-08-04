Fixed the chat progress view hiding a reply the agent had already delivered.

When a Stop hook fired or a background task finished while a step was still open, the notification woke the agent back up, and the work it did next pulled the wrap-up message it had just written into that step -- where it collapsed down to a single italic caption line, or vanished entirely behind the step's expand arrow. The longer the reply, the more there was to lose.

A notification now marks the end of the agent's turn, so anything said before it stays in the conversation as a full message no matter what the agent does afterwards.
