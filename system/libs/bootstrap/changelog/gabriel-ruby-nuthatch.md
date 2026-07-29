- The initial chat agent is now created with an explicit fast-mode setting, read
  from the workspace's fast-mode decision at `data/.state/fast_mode_decision.json`
  (written by the system interface when the user answers the fast-mode prompt).
  On a first boot there is no decision yet, so the opening conversation starts
  fast; a workspace restored with a recorded answer honours it instead.

- Bootstrap parses that file directly rather than importing the system interface,
  which owns the format. Bootstrap deliberately carries almost no dependencies --
  it runs before supervisord and must stay light -- so the path is repeated on
  both sides rather than shared. The file is a single boolean and an absent file
  means unanswered, which keeps that duplicated parsing to a few lines.
