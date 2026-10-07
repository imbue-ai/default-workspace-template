The type-check ratchet runs `ty` 0.0.85 (was 0.0.24); the pin's comment no longer claims newer versions are off limits.

The test server helper is annotated as returning `Generator[...]` rather than `Iterator[...]`, which 0.0.85 reports as a deprecated overload of `contextmanager`. Finding the nearest free desktop cell keeps its `min(..., key=lambda ..., default=None)` and carries a targeted `ty: ignore[invalid-argument-type]`: `ty` folds the default's `None` into the key's parameter type, an open `ty` bug (astral-sh/ty#4016).
