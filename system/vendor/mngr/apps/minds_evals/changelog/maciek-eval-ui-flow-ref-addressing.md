UI flows: an element the page lists with no accessible name can be addressed by its snapshot ref.

- The verification agent may address an element the accessibility tree lists with no name at all
  (a checkbox with no label, say) by the `[ref=eN]` the snapshot prints for it, instead of ending the
  flow with "no usable action". The step script checks the ref against a fresh snapshot before acting
  on it and refuses one that no longer sits on the role the agent read, recording that as a step that
  did not run.

- Such a step carries the ref on its `log.jsonl` line as `target_ref`, and the judge's digest marks
  it and notes that a control with no accessible name is an accessibility defect of the delivered
  app, recorded for a separate measure and, unless the declared actions or the `expect` call for
  accessibility, not to be counted against the flow.

- A decision the agent makes that cannot be acted on is explained in the decision's own words: in the
  driver log, in the manifest entry's detail, and on a `(no usable action)` step in the flow log
  that shows the page the decision was made on.

- The flow lab's todo fixture gains `?unnamed=1`, which strips the label association from each
  task's checkbox; the release test of the real agent runs on it too.
