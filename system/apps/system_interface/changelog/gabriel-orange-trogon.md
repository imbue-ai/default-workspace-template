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
