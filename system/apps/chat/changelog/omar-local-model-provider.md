- Added a "Local model" provider to the provider chooser, which runs a chat on a model you host yourself through Ollama, LM Studio, llama.cpp, or any other OpenAI-compatible server. Its form lists the models of the servers it finds on your computer, takes another server's address and optional API key instead, and takes the model's context window (32,768 tokens to start).

- Connecting a local model first asks it for one tool call, and refuses a model that answers without one, since such a model runs nothing in a chat. It also refuses a model its server loaded with a context under 16,384 tokens, which is less than a workspace's own instructions need, and says how to raise Ollama's.

- The model picker now lists the model a chat is running on when no bundled catalog names it, so a local model shows in the model bar and its picker.
