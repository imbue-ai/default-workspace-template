/** Candidate rewriting (docs/system/specs/screen/spec.md section 5.3). In this wave ``connect-info`` answers no
 *  endpoints, so a server candidate is used as given; the rewrite to published endpoints lands with the desktop's
 *  brokers. */

import type { ConnectInfo } from "./state";

export function candidatesToAdd(candidate: RTCIceCandidateInit, connectInfo: ConnectInfo): RTCIceCandidateInit[] {
  if (connectInfo.candidates.length === 0) return [candidate];
  return [candidate];
}
