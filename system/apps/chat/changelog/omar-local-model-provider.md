- Added a "Local model" provider to the provider chooser, which runs a chat on a model you host yourself through Ollama, LM Studio, llama.cpp, or any other OpenAI-compatible server. Its form lists the models of the servers it finds on your computer, takes another server's address and optional API key instead, and takes the model's context window (65,536 tokens to start).

- Connecting a local model first asks it for one tool call, and refuses a model that answers without one, since such a model runs nothing in a chat. It also refuses a model its server loaded with a context under 32,768 tokens, since a new chat's first request alone is about 24,000, and says how to raise Ollama's.

- The model picker now lists the model a chat is running on when no bundled catalog names it, so a local model shows in the model bar and its picker.

- While a message the agent already has is still waiting on the model's first reply, the status row under it now reads "Waiting for <model>…" in place of "Sending to <harness>…". A model hosted on your own computer can take over a minute to read a new chat's instructions, and pi records nothing of that turn until the reply.
