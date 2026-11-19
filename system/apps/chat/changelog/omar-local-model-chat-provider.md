- Added a "Local model" provider to the provider chooser, which runs a chat on a model you host yourself through Ollama, LM Studio, llama.cpp, or any other OpenAI-compatible server. Its form lists the models of the servers it finds on your computer, takes another server's address and optional API key instead, and takes the model's context window (65,536 tokens to start).

- Connecting a local model first asks it for one tool call, and refuses a model that answers without one, since such a model runs nothing in a chat. It also refuses a model its server loaded with a context under 40,960 tokens, since a new chat's first request alone is about 29,000 and the window has to hold a reply too, and says how to raise Ollama's.

- The model picker now lists the model a chat is running on when no bundled catalog names it, so a local model shows in the model bar and its picker.

- While a message the agent already has is still waiting on the model's first reply, the status row under it now reads "Waiting for <model>…" in place of "Sending to <harness>…". A model hosted on your own computer can take over a minute to read a new chat's instructions, and pi records nothing of that turn until the reply.

- A local model that reasons before it answers is signed in as a reasoning model: a chat shows its thinking folded away above the reply, as for any other provider, and offers its thinking levels. For Ollama the "off" level stops the reasoning.

- A message sent to a chat on a local model is first checked against its model server. When the server does not answer within five seconds (the computer is asleep, or the model server is not running) the message is not sent: a notice says what to check and the text stays in the composer. A server that drops out mid-reply ends the turn with the same words in place of pi's "Connection error."

- A local model's turn that ends with no answer and no action now says so in the chat, with what to try, in place of an empty reply.
