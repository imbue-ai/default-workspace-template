Grading fidelity: a trial whose verifier scored no structural gates is now errored rather than graded
0. The gates are programmatic criteria with no judge behind them, so a `gates` dimension carrying no
readable criterion means the verifier did not run, which the reward composition cannot tell apart
from an agent that failed the gates -- both leave the trial gated shut. A gate that ran and said no
is still a legitimate 0. This joins the other grading-infrastructure failures (a judge error, an
unparseable rewardkit output, an untrustworthy `case.json`, unmeasurable outcome evidence), all of
which leave no reward file so harbor errors the trial.

The `avg_word_count_baseline` eval-config key is gone. Nothing has read it since the message-length
guard replaced the average-words-per-turn measure with per-message limits; a config that still sets
it loads unchanged, with the key ignored.
