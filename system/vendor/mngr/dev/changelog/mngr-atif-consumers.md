`specs/minds-analytics/redaction-contract.md` now states the field dispositions
for the ATIF-shaped common-transcript records (`header`, `step`, `observation`)
next to the legacy ones, including the envelope rename (`source` -> `emitter`),
which token counters survive the strip, and what happens to the stream header
on the way to the lake (skipped as framing, not counted as a dropped line).
