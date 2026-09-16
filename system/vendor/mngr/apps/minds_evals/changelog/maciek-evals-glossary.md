Adds a glossary of minds-evals terminology at `apps/minds_evals/docs/glossary.md`, linked from the top of the README, which now asks every PR that adds, renames or overloads a term to update the glossary in the same change.

It defines the vocabulary the README, the specs and the code share, and each entry names the module, class or key that carries the term, so a word met in prose can be found in code. Harbor and rewardkit vocabulary (task, trial, job, verifier, dimension, criterion, judge, oracle, regrade) has its own section, followed by our own terms grouped by the stage of a run they belong to: runs and arms, inside a trial, the conversation, stepped cases, expectations and evidence, UI flows, grading, usage and cost, scheduled CI.

Terms already slated to change (verification agent, flow lab, harness spend, gate, flow) end in an *Earmarked* line naming what they will become, so new names are not built on them.

A closing section lists the words that mean more than one thing here (step, harness, verifier, check, gate, flow, run, agent, environment, outcome, state) and says which sense each entry uses.
