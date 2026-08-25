#!/bin/bash
# Run the rewardkit verifier (gates + quality, plus outcome when the case
# declares expectations), then compose the final gated reward. rewardkit owns
# all judging and scoring; finalize.py combines the dimension scores (reward =
# quality, or an even split of quality and outcome, zeroed unless every gate
# passed) and distinguishes a graded 0.0 from a grading-infrastructure failure
# (judge API error / no parseable reward file / unmeasurable outcome evidence),
# leaving no reward file in the latter case so harbor errors the trial instead
# of scoring it 0.
set -euo pipefail

# Rebuild the judged transcript from the raw event stream at grade time (so
# `harbor trial regrade` re-scores captured trials under the current rendering):
# one message-per-block rendering the judge scores conciseness against, per
# individual agent message rather than per merged turn.
python3 /tests/render_judge_transcript.py

# Cases that declare expectations get an outcome dimension; its judge grades against the case's
# ground truth, rendered here for the same regrade reason.
if [ -d /tests/outcome ]; then
  python3 /tests/render_expectations.py
fi

# rewardkit exits nonzero on a hard judge failure and may not write reward.json;
# tolerate its exit code here and let finalize.py inspect the outputs.
uvx --from 'harbor-rewardkit==0.1.*' rewardkit /tests --workspace /app || true

# finalize.py exits nonzero (aborting under set -e) on a grading failure, after
# removing any reward file so the trial errors rather than grading a fake 0.0.
python3 /tests/finalize.py
