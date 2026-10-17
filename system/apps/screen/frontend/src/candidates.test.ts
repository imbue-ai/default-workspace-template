/** Candidate handling in this wave: the server's candidate is used as given. */
import { expect, it } from "vitest";

import { candidatesToAdd } from "./candidates";

const candidate: RTCIceCandidateInit = {
  candidate: "candidate:1 1 udp 2130706431 172.17.0.2 59000 typ host",
  sdpMid: "0",
  sdpMLineIndex: 0,
};

it("passes a server candidate through whether or not endpoints were announced", () => {
  expect(candidatesToAdd(candidate, { ice_servers: [], candidates: [], expires_at: null })).toEqual([candidate]);
  expect(
    candidatesToAdd(candidate, {
      ice_servers: [],
      candidates: [{ host: "203.0.113.5", port: 59000, protocol: "udp" }],
      expires_at: null,
    }),
  ).toEqual([candidate]);
});
