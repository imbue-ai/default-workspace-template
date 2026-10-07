The browser on a phone (`docs/system/blueprint/desktop-interface/plan-phone-interface.md`):

- The viewer fills the dynamic viewport (`100dvh`) inside the screen's safe areas (`viewport-fit=cover` with `env(safe-area-inset-*)` padding), so the streamed browser is sized to the phone's window.

- Rotating the device reports the pane's size to the stream again, even when the rotation settles on the size already reported.
