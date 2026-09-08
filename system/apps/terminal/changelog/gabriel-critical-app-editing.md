`app.toml` gains the terminal's `[preview]` table: a preview boots `terminal-app --no-register` with the ttyd page and the instances API on two free ports (`main` and `sidecar`), over a copy of `data/.apps/terminal` and a scratch state directory, and probes `/_instances`.

`terminal-app --no-register` serves ttyd and the instances API without re-pointing the live terminal's registry row, for a preview on free ports.
