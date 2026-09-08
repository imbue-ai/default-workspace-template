# Captured agy step payloads

Real `steps` rows lifted from a live agy conversation store (agy 1.1.20), base64-encoded.

They exist because every agy test before them built its payloads with our own
`build_step_payload(...)` helper -- and a synthetic body never reproduced the shape that broke
the decoder. A bug that corrupted 100% of agy tool results passed CI for the life of the
harness because no test had ever seen a real one.

Re-capture with the procedure in `docs/design/antigravity-transcript-schema.md`.
