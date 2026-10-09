The type-check ratchet runs `ty` 0.0.85 (was 0.0.24); the pin's comment no longer claims newer versions are off limits.

The test server helper is annotated as returning `Generator[...]` rather than `Iterator[...]`, which 0.0.85 reports as a deprecated overload of `contextmanager`. Finding the nearest free desktop cell answers `None` for no candidates before taking the `min`, rather than passing `min` a `default=None` that 0.0.85 folds into the key's parameter type.
