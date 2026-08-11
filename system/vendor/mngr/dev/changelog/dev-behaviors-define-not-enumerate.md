The `behaviors` skill gains a normative subsection, "Prose style: define categories, don't enumerate them".

Descriptive prose (Feature/Rule/Scenario/Scenario Outline description slots and every `.md` file) must now define each category by the property that makes it that category, never by volunteering example members, because enumerated samples rot and are silently promoted by consuming agents into contracts.

A load-bearing example may survive only when marked `e.g. (wlog)` or `without loss of generality`; elimination is preferred to marking.

Normative Gherkin steps and `Examples` tables are explicitly out of scope - concrete values there are the observable contract.

A DRY corollary places subtree-wide statements once in the most-scoping `overview.md`, and a short before/after pair calibrates authors.
