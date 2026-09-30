# Phone mock

The interactive mock `plan-phone-interface.md` was iterated in: one HTML file with its CSS and JS inline, plus the icons, avatar and wallpaper it draws. It is the reference for measurements, spacing and copy the plan does not restate; where the mock and the plan disagree, the plan wins.

Serve the directory and open `mock.html` in a browser sized like a phone, or on a phone over the local network:

    python3 -m http.server 8765 --bind 0.0.0.0

`mock.html` lands on the most recently shown window; `mock.html?home` starts on the home grid. It covers the bar and sheets, the home grid, the chat app (drawer, transcript with tool chips and step timelines, composer, model menu, provider sign-in), the file viewer, and a terminal placeholder. Its data is fixtures, not a workspace.
