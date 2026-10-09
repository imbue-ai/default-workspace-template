The type-check ratchet runs `ty` 0.0.85 (was 0.0.24).

The registry reader's skipped-row log line no longer ignores a `ty` rule: 0.0.85 types the row-name lookup that 0.0.24 could not. The manifest tests build their launch path from a typed helper instead of reaching into the manifest dict behind mypy-style `type: ignore` comments, which `ty` stopped honoring in 0.0.25.
