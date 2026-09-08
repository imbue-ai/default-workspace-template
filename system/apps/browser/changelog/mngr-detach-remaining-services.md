The browser app now runs chromium, Xvfb, xdpyinfo, pgrep, pactl and the layout helper in their own sessions, so none of them can stop the browser service by touching the workspace's terminal. Chromium and Xvfb are still stopped the same way, by their own handles.

Two spawns stay attached on purpose: the clipboard bridge, whose payloads are binary and arrive on the child's stdin, and the shared PulseAudio daemon, which is meant to outlive any one browser and would otherwise never be cleaned up at all.
