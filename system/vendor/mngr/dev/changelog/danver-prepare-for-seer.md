Decomposed bug investigation into four shared skills, so that the local investigation flow and the (unmerged) mngr-seer automated pipeline can run the same method and both benefit from any improvement to it.

New shared skills, each pure -- no credentials, no network, no commands -- so an air-gapped agent can invoke them unchanged:

- `classify-failure`: read a failure's text, name its shape, and decide which observability stores can and can never hold its evidence.

- `correlate-evidence`: join evidence across stores on the correlation keys, navigate the client/server identity gap, and decide what an absence actually means before trusting a zero.

- `diagnose-mechanism`: turn evidence plus source into a named failure mechanism with the competing hypotheses the evidence does not separate. Previously existed only inside mngr-seer.

- `calibrate-confidence`: decide whether a diagnosis is actionable, or name the exact logs that would settle it. Previously existed only inside mngr-seer.

`investigate-bug` is rewritten as the local composition: a six-step flow that invokes the four shared steps and keeps only what is local and privileged -- the live server-health probe, the bug-report move, access boundaries, and the living-document protocol. Its content moved rather than changed; the visible gain is that a stalled local investigation now has a defined artifact to produce instead of trailing off.

New design docs at `specs/bug-investigation-pipeline/`: `spec.md` records the two-consumer model (local, human-driven, privileged versus automated, sandboxed, batched, hands-off but human-reviewed), the acquisition/interpretation/emission seam, and why the shared steps are skills rather than reference documents -- a skill is obligatory to read, a reference document is not. `seer-composition.md` is the binding contract for the automated consumer: which skills each mngr-seer agent composes, the multi-store fetching and search-record its orchestrator must supply, and how shared skills reach a sandbox that is outside this checkout.

No behaviour change for any running system: this branch touches only skills and specs.
