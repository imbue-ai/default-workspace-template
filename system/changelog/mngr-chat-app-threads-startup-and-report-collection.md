- The bug-report collector no longer asks mngr for a stopped agent's pane (it has none), and reads panes and chat transcripts four at a time instead of one after another. On a 35-agent workspace that was 71 serial mngr calls and about two minutes; on a loaded one it overran the 300 s collection budget and the report lost its whole archive.

- mngr is pinned to a build that lets the chat app name the providers it compacts agents on.
