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

# The evidence directory is a declared artifact, and the driver creates it on every real trial that
# reaches a workspace. Create it here too, even for a case with nothing to verify, so the artifact
# collector never has to log a (harmless but alarming) failure to find it.
mkdir -p /logs/agent/verification
printf 'oracle run: no real workspace, so nothing was probed\n' > /logs/agent/verification/README.txt
$verification_evidence_sh
