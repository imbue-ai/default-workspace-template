Failing to pull a browser's live pane is no longer reported as an error.

When the fleet starts a browser it optimistically tries to split its live view
into the current agent's chat. That split only lands when a client is actually
watching this agent's chat, so for a background or sub-agent it routinely does
not -- and the previous message ("I couldn't open its live pane here...") framed
that expected outcome as a failure, implying something had broken when the
browser was up and fully drivable from the CLI.

The fallback is now informational and goes to stdout rather than stderr:
"browser <name> is ready. To watch it live, open it from the '+' menu (New
browser -> <name>) in the side panel." The pane is a convenience, not a
precondition for using the browser.
