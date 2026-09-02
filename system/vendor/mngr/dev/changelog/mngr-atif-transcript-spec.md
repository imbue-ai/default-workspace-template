Added `specs/atif-transcript-alignment/spec.md`: a design spec that realigns the agent-agnostic common transcript with Harbor's Agent Trajectory Interchange Format (ATIF v1.7).

The spec redefines the on-host stream as JSONL of ATIF-shaped records (header/step/observation) at full fidelity (complete tool arguments and outputs, reasoning content, system steps with the compaction convention), adds a doc-builder in `libs/mngr` that assembles a valid single-document ATIF trajectory (with embedded subagent trajectories distinguished as mngr vs native via `extra.subagent_kind`), vendors harbor's ATIF pydantic models pinned to ATIF-v1.7, and migrates all five emitters and all consumers with no compatibility layer.

Marked the prior `specs/common-transcript-standard/spec.md` (OTel GenAI vocabulary alignment) as superseded by the new spec.
