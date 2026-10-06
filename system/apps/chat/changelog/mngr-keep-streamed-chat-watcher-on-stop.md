An open chat no longer stops showing new events after its agent is briefly reported stopped.

Before, the moment the observer reported a chat's agent dead, the chat app dropped the chat's transcript watcher. That watcher is the only thing feeding the page's live stream, and the stream kept sending keepalives, so the page looked connected while nothing new appeared until a reload. A live agent can be reported dead for a moment: an automatic rename moves the agent's tmux session before it rewrites the agent's name, and a listing in between finds no session. The next message sent from the chat rebuilt the watcher, but everything written in the gap never reached the page.

Now a chat whose agent dies is released only when nobody is streaming it, at the first observe event after its last stream closes. The memory guarantee for stopped chats is unchanged: a stopped chat that no window shows is still dropped at the same observe event as before.

Removing a chat's active agent now drops the whole chat's resident transcripts, archived segments included. A chat whose active agent is gone never reads as stopped, so nothing else would release those segments.
