`forward_port.register` takes an `isolated_instance` tag (`{app, kind, chat?}`) and writes it onto the row, as `serve_isolated_instance.py` does for the window of a preview or a mock's frame. Like `theming_mode`, every registration is authoritative for it, and a manifest registration clears it.

A manifest launch path's `chat_draft_params` is copied onto the row.
