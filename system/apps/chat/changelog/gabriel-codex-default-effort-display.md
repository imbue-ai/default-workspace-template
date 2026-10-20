A Codex chat whose effort was never picked now shows the effort it is running at, the model's default (Medium for GPT-6-Sol), instead of none.

Before, the model menu's effort control showed no current effort for such a chat: the slider sat at Low and the model chip left the effort out. Codex records no effort until one is picked, and the chat app did not pass on the per-model default that Codex reports in its model list. Each model option now carries that default, and the menu shows it whenever the chat has recorded no effort.

Switching a chat like this to another model now keeps the default effort it was running at, if the new model offers it, instead of dropping to the lowest.

Starting a new chat from such a chat on another account of the same provider, keeping its model, now starts it at that default effort too, instead of failing to apply the model because no effort was named.

Fast mode can now be turned on or off for such a chat, including the switch Auto makes after its turn limit. The change was refused because it carried no effort, so fast mode stayed as it was. A model with a default effort no longer needs an effort named.

A newly picked model that does not offer the current effort, in the model menu or the switch dialog, now starts at its own default effort instead of the lowest one.
