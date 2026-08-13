#!/bin/bash
# Oracle solution: write a canned near-perfect transcript (and matching state)
# into /logs/agent/ without booting Minds, so `harbor run -a oracle` exercises
# generation, environment build, artifact transfer, and verification
# end-to-end. Because the judges are LLMs, oracle runs assert reward >= 0.8
# rather than exactly 1.0.
set -euo pipefail

mkdir -p /logs/agent/snapshots

# The canned conversation is already clean (user/agent pairs), so the raw
# transcript and the graded conversation file are identical for the oracle.
cat > /logs/agent/full_transcript.jsonl << 'MINDS_EVALS_TRANSCRIPT_EOF'
$transcript_jsonl
MINDS_EVALS_TRANSCRIPT_EOF

cat > /logs/agent/conversation.jsonl << 'MINDS_EVALS_CONVERSATION_EOF'
$transcript_jsonl
MINDS_EVALS_CONVERSATION_EOF

cat > /logs/agent/state.json << 'MINDS_EVALS_STATE_EOF'
$state_json
MINDS_EVALS_STATE_EOF

printf 'oracle run: no real workspace, so no snapshots were taken\n' > /logs/agent/snapshots/README.txt
