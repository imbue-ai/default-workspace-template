The workspace agent's always-loaded instructions are retuned for the model that
actually runs them. `AGENTS.md` and the Engineering Subordinate output style
carried a layer of text written for weaker, chattier models, and on the current
one that text cost output quality rather than buying it.

`AGENTS.md` no longer opens by insisting the agent follow it and report its own
lapses, and no longer asks for a reflection pass at the end of every response.
The sections describing a checkout this workspace does not have are gone with
them: there was no root justfile for the `just test` recipe to reach, no pull
request for acceptance tests to defer to, and no `data_types/` or `interfaces/`
directory for the start-of-task reading pass to read. What remains about tests,
ratchets and fixtures is scoped to the three vendored projects that ship pytest
suites. In their place the file states the scope rule plainly -- hold the scope
the user set, finish the whole task, report it done only when it is -- and closes
with a note to keep responses and written files to the length the work needs.

The output style now agrees with itself. It used to ask for a sentence saying
what the agent was about to do, then delete that sentence in a pre-send check,
then ban the phrasings for it. The opening line stays; the flattery and the
sign-off go. Its compression rules told the agent to drop articles and answer a
Portuguese speaker in compressed Portuguese, three lines above an override
restoring articles and full sentences, with a worked example written in the
broken style -- so chat replies to non-technical users inherited the broken
grammar. The register rules that block was protecting are kept and stated
straight. The five-item list cap, the per-turn state restatement (the progress
timeline already does that) and both review-before-sending passes are gone.

Skill descriptions ride in every request, and ten of them were a sentence or
less. They now carry the contract a caller needs before opening the file: which
directories `file-sharing` can reach and that access is granted per path by a
permission prompt, that `blueprint` writes no code and ends only when the user
says so, that a `launch-task` sub-agent costs a full context re-establishment and
is not the way to check your own work. Descriptions that opened with an urgency
marker no longer do, conversational instructions in them have moved into the
skill bodies where they fire when they are relevant, and the lists of
near-synonyms and sample user phrasings are replaced by the category of intent
they were approximating.
