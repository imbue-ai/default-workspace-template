- A local-model account now offers every model of its server, so a chat on one switches between them from its model menu like a chat on any other provider. A model is checked for tool calling the first time a chat switches to it, and one that fails is refused with the reason while the chat stays on the model it had.

- A model is listed once, however many context sizes it runs at. Ollama runs a model at another context size only as a named copy of it, which filled the list with near-identical names (`qwen3:8b`, `qwen3-32k`, `qwen3-40k`). Those copies are now folded into their model: the Local model form has one dropdown for the model and one for its context size, and a chat's model menu has a Context window row beside its Model row. Picking a size that has no copy yet makes one on the server (instant, sharing the model's weights), so nobody has to run `ollama create` first.

- The Local model form no longer offers a model that cannot run a chat. Models that list no tool calling, or whose own context limit is under 40,960 tokens, are named under the picker with why.

- When your computer runs no model server, the Local model form now says you may need to install Ollama and that Imbue Studio can do it for you, with a link that opens Studio's Settings at Local models. The "Your model server didn't answer" notice a failed send shows says the same and carries an **Open Local models settings** button.

- A local-model account signed in before this change picks all of this up the first time a chat's model menu is opened, with no need to sign in again.

- While a local model's first reply waits for your computer to read the model into memory, the chat's status line says so ("Loading qwen3:8b (40K) on your computer. The first reply can take a minute…") in place of "Thinking…".

- The Local model form says up front that a model on your own computer answers more slowly than a provider's, and suits background jobs such as an app's refreshes best.
