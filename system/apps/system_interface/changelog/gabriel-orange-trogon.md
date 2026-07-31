# Multi-client robustness for the system interface

Fixes for the misbehavior seen when two browser clients (the desktop app and a
shared browser tab) drive the same workspace, plus the single-client chat-paging
wedge found alongside it.

A chat tab whose agent has no Claude session no longer hammers the terminal
screen-capture endpoint. The capture result is cached whether it succeeded or
failed, so a failure can no longer refire on every redraw (which previously
produced tens of thousands of 404s a day for a destroyed agent's stale tab); an
automatic retry happens at most every 30 seconds, and there is an explicit Retry
button. A chat that 404s because the agent list was momentarily empty now
recovers on its own as soon as the agent reappears, instead of staying wedged
until the page is reloaded.

A chat tab whose agent has actually been destroyed -- from this client, another
client, or the CLI -- now says so. The tab is kept rather than silently removed,
so you keep the context that the agent existed, and it renders "This agent was
destroyed." with a Close button and makes no further requests of any kind.
Closing it takes the tab out of the saved layout, so it stops reappearing on
every restore. The tab is only declared dead when the agent is missing from
consecutive agent-list updates *and* its transcript comes back not-found, so a
momentary hiccup in agent discovery cannot tombstone a live chat.

Scrolling a long transcript no longer corrupts its own loaded window. Pages that
arrive after the view has been reset (an SSE reconnect, or a jump to a far scroll
position) are discarded instead of being spliced in, pages that don't join up
with what's loaded are rejected, and a window that has drifted out of step with
the live end of the conversation is snapped back onto it. Every paging request is
now timeboxed, so a hung one can no longer freeze a chat under a permanent
"Loading messages..." overlay, and a run of pages that achieve nothing backs off
instead of retrying on every frame.
