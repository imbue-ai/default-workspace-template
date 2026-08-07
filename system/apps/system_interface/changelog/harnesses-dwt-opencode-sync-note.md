Documented a known limitation of the opencode harness: the chat model bar and the opencode
TUI (`opencode connect`) do not stay in sync, and can't without patching opencode
(v1.18.14). The TUI keeps its model choice client-local and ships it inline per prompt: a
bar switch sets the server session model but doesn't move the TUI (and a TUI-sent turn
overrides it), and a TUI `/model` reaches the bar only once a turn records it. The chat bar
is authoritative for chat-UI-sent messages; the TUI is a separate client. (Details in the
`OpenCodeModelResolver.switch` docstring.)
