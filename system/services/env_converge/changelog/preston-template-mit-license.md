The manifest validator now fails a template whose README still carries the
`MINDS_TEMPLATE_LICENSE` placeholder.

The License section is generated during assembly, before the publish flow has
asked the user how they want the work licensed. Treating the leftover
placeholder as a validation failure is what stops a repo shipping with that
question unanswered -- and with the literal token printed on its landing page.

It sits with the existing unfinished-placeholder checks, so one validator run
remains a complete gate rather than one of several a caller has to remember.
