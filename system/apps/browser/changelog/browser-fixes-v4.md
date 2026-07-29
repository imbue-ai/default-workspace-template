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

----

The live view is now KasmVNC, and the browser pane shows the real Chromium.

Each browser gets its own KasmVNC session -- an X server that is also the web
client streaming it -- and Chromium is launched headful into it. The pane embeds
that client, so what you see is the whole browser window: its real tab strip,
its real back/forward buttons, its real address bar.

**Input works properly now.** Mouse and keyboard travel down the VNC connection
and are injected as X events at the display level, so native right-click context
menus, native `<select>` dropdowns and date pickers, text selection and real
click-drag all behave like a normal browser. None of those could work before:
CDP's page-scoped input events cannot reach browser-native UI.

**What's gone from the pane:** the hand-built tab strip, back/forward/reload
buttons and address bar. They existed only because the old CDP screencast
captured the page viewport and not Chromium's own chrome, so they had to be
rebuilt in HTML. Chromium's own chrome replaces all of them.

**What's unchanged:** the "An agent has control" overlay, Take control, the
"You have control" bar and Return control, the starting spinner, and the crashed
state. Agents still drive over CDP exactly as before, and the fleet's naming,
cap, and `+` menu behave identically.

Each browser's display is registered as its own workspace service,
`browser-<name>`, and the pane points at it. The KasmVNC client builds absolute
`/assets/...` URLs and derives its own websocket URL from `window.location`, so
it has to be served at the root of its own prefix; giving it one is simpler than
proxying it and rewriting both.

Sessions are started through KasmVNC's own `vncserver` launcher rather than a
hand-built `Xvnc` command line, and a `~/.kasmpasswd` is written first. That is
what upstream's container startup and LinuxServer's baseimage both do:
`vncserver` merges the YAML config hierarchy, generates the SSL certificate, and
spawns `Xvnc` with computed arguments. Driving `Xvnc` directly means reproducing
all of that by hand.

Two consequences worth knowing:

- There is no window manager on these displays, so `document.hasFocus()` is
  false in the page. Clicking and typing work; the JS Clipboard API and some
  autofocus behaviours do not.
- Chromium's address bar can reach `chrome://` pages, downloads and devtools.
  The old view had no address bar, so that surface was previously unreachable.

----

Browser tabs now have a Destroy button, matching agent and terminal tabs.

Closing a browser tab only hid the pane -- the browser kept running and kept
holding one of the fleet's slots against its cap. The trash button beside the
tab's close button now closes the browser in the fleet: its Chromium is killed
and the slot is freed. Same icon, position and confirmation dialog as destroying
an agent or a terminal; the plain close button still just hides the pane.

Destroy is deliberately not gated on who is using the browser. If an agent is
driving it, the daemon's close path releases every queued agent and tells the
one driving that the browser is gone, so it reports a clear error rather than
hanging -- the same way a crashed browser already behaves.

----

A browser's X server no longer survives the browser service being OOM-killed.

The session stays in `browser-service`'s process group, so supervisord's existing
`stopasgroup`/`killasgroup` reaps it on stop or restart. For the case supervisord
cannot cover -- earlyoom kills the service directly and never signals the group --
the service also sweeps its own display range at startup, before restoring
anything, and kills whatever it finds there. At that point it owns no display, so
anything in range is an orphan from a previous run.

----

The browser pane got its focus indicator and tips card back.

While keyboard focus is inside the live view -- so keys go to the remote
browser -- the pane shows the green inset ring and a "Keys go to the browser"
flag in the control bar, with an explicit "Lose browser focus" button as the
exit, matching the old canvas viewer. A "View tips" button opens the usage
card; its first bullet documents which keyboard shortcuts are reliable
per platform (Cmd combos on macOS are partly unreliable -- use Ctrl+T/W as the
workaround -- while plain letters and Cmd+C/V/A/E/F work), followed by the
sharing/queue, handoff and saved-logins notes unchanged.
