The project is type-checked: its ratchets run `test_no_type_errors` (`ty`, pinned to 0.0.24 as the shell and the chat pin it), and its `pyproject.toml` makes the directory its own `ty` project (an empty `[tool.ty]` table).

- The broker's JWKS cache keeps only RSA public keys: a refreshed JWKS entry that carries a private key is dropped, so its `kid` is unknown rather than handed to `jwt.decode`, which cannot verify with it.

- The relay-assignment body and the grants file's scopes are read as objects with string keys, and a grants string list is built from its string entries.

- Certificates' SAN names are read through `get_extension_for_class(SubjectAlternativeName)`; the request log reads the real client IP from werkzeug's `Headers`; `/_health` answers its JSON as a flask `Response`.
