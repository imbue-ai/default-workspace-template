Evals run on harbor 0.22.0 (from 0.21.0), and the verifier grades with harbor-rewardkit 0.2.0 (from
0.1.7). Generated datasets, recorded trials and the `task.toml` schema (still `1.4`) are unchanged,
so trials recorded under 0.21 stay loadable and regradable.

The rewardkit pin is exact (`==0.2.0`) in both places that carry it, where it used to be the range
`==0.1.*`. `uvx` resolves the verifier's rewardkit in an isolated tool environment that honours no
cooldown, so a range let two trials in one run -- or a trial and a later regrade of it -- be graded
by different builds with nothing in the output to say so.

Regrade documentation now matches the commands. `harbor trial regrade` and `harbor job regrade` both
default to a local docker daemon (`-e modal` is what puts them on the environment the run used), take
either one task directory or a dataset of them in `-p` (matched to the trial by `[task].name`), and
grade with today's verifier rather than the one that recorded the trial. `-o` is a parent directory
in both, so only `job regrade` writes something `harbor view` lists as a job; the single-trial form
gets its own ignored `regrades/` directory. Gates reproduce exactly; judge criteria are sampled, so
an unchanged trial still moves by whole likert points on a regrade. Compare regrades against
regrades.

Two guardrails the new rewardkit makes load-bearing. It recurses below a dimension directory, so a
nested directory now joins that dimension's weighted mean rather than being ignored, and a judge
toml dropped at the criteria root becomes a dimension of its own. And it refuses a judge whose
criteria names collide -- names it derives from the first 40 characters of a description when one is
not given, so every criterion in the per-case outcome judge must carry an explicit `name`.
