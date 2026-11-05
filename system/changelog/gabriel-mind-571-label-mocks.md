`forward_port.register_manifest(path, manifest_path, url, overrides=...)` registers an app from its manifest, as `--manifest` does, with its `internal` and `stop_when_no_windows` replaced on the row until the next plain registration. `build-app`'s mock frame uses it to hide the app it frames and keep it running while only the frame has a window.

`workspace_bare_scripts/frame_kind.py` holds `FrameKind` (`change` or `mock`), what a labeled preview frame says the page inside it is, so the frame substrate and the wrapper server, which runs under the system `python3`, share one type.
