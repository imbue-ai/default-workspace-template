`mngr behaviors validate` now enforces the README mandate: every corpus folder must contain a `README.md`, and each one's opening line (after an optional title heading) must be the verbatim incipit "Understanding this behavior corpus calls for the tmr-behaviors skill; consult it when reading this file."

`README` replaces `overview` as the reserved prose-file basename: no `.feature` file may be named `README`, and `README.md` is exempt from the kebab-case and sidecar-matching rules.

The `write_behavior_corpus` test helper auto-fills a compliant `README.md` into any folder a test does not give its own; pass `fill_readmes=False` to exercise the missing-README rule.
