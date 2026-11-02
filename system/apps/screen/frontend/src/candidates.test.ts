/** Candidate rewriting (spec section 5.3): every server candidate becomes one copy per matching endpoint with the
 *  address and port replaced and a distinct foundation, the original dropped unless no endpoint is known. */
import { describe, expect, it } from "vitest";

import { candidatesToAdd, parseCandidateLine } from "./candidates";
import type { ConnectInfo } from "./state";

const UDP_HOST = {
  candidate: "candidate:2399948542 1 udp 2130706431 172.17.0.3 59000 typ host",
  sdpMid: "0",
  sdpMLineIndex: 0,
  usernameFragment: "abcd",
};
const TCP_HOST = {
  candidate: "candidate:1037416384 1 tcp 1671430143 172.17.0.3 59000 typ host tcptype passive",
  sdpMid: "0",
  sdpMLineIndex: 0,
};

function info(candidates: ConnectInfo["candidates"]): ConnectInfo {
  return { ice_servers: [], candidates, expires_at: null };
}

describe("candidatesToAdd", () => {
  it("rewrites a udp candidate to the published udp endpoint and drops the original", () => {
    const added = candidatesToAdd(UDP_HOST, info([{ host: "127.0.0.1", port: 32771, protocol: "udp" }]));
    expect(added).toEqual([
      { ...UDP_HOST, candidate: "candidate:23999485420 1 udp 2130706431 127.0.0.1 32771 typ host" },
    ]);
  });

  it("rewrites a tcp candidate keeping its tcptype", () => {
    const added = candidatesToAdd(TCP_HOST, info([{ host: "relay-us1.example", port: 40123, protocol: "tcp" }]));
    expect(added).toEqual([
      {
        ...TCP_HOST,
        candidate: "candidate:10374163840 1 tcp 1671430143 relay-us1.example 40123 typ host tcptype passive",
      },
    ]);
  });

  it("gives each copy a distinct foundation when several endpoints match", () => {
    const added = candidatesToAdd(
      TCP_HOST,
      info([
        { host: "203.0.113.5", port: 32772, protocol: "tcp" },
        { host: "relay-us1.example", port: 40123, protocol: "tcp" },
        { host: "relay-us1b.example", port: 40123, protocol: "tcp" },
      ]),
    );
    const foundations = added.map((entry) => parseCandidateLine(entry.candidate ?? "")?.foundation);
    expect(foundations).toEqual(["10374163840", "10374163841", "10374163842"]);
    expect(added.map((entry) => parseCandidateLine(entry.candidate ?? "")?.address)).toEqual([
      "203.0.113.5",
      "relay-us1.example",
      "relay-us1b.example",
    ]);
  });

  it("matches by protocol when both protocols share one host port", () => {
    const both = info([
      { host: "127.0.0.1", port: 59000, protocol: "udp" },
      { host: "127.0.0.1", port: 59000, protocol: "tcp" },
    ]);
    expect(candidatesToAdd(UDP_HOST, both).map((entry) => entry.candidate)).toEqual([
      "candidate:23999485420 1 udp 2130706431 127.0.0.1 59000 typ host",
    ]);
    expect(candidatesToAdd(TCP_HOST, both).map((entry) => entry.candidate)).toEqual([
      "candidate:10374163840 1 tcp 1671430143 127.0.0.1 59000 typ host tcptype passive",
    ]);
  });

  it("drops a candidate whose protocol no endpoint carries", () => {
    expect(candidatesToAdd(UDP_HOST, info([{ host: "relay.example", port: 40123, protocol: "tcp" }]))).toEqual([]);
  });

  it("compares the protocol case-insensitively", () => {
    const added = candidatesToAdd(UDP_HOST, info([{ host: "127.0.0.1", port: 32771, protocol: "UDP" }]));
    expect(added).toHaveLength(1);
  });

  it("uses the server's candidate as given when no endpoint is known", () => {
    expect(candidatesToAdd(UDP_HOST, info([]))).toEqual([UDP_HOST]);
  });

  it("passes an end-of-candidates marker through", () => {
    const marker = { candidate: "", sdpMid: "0", sdpMLineIndex: 0 };
    expect(candidatesToAdd(marker, info([{ host: "127.0.0.1", port: 1, protocol: "udp" }]))).toEqual([marker]);
  });

  it("caps the rewritten foundation at 32 characters while keeping it distinct", () => {
    const long = { candidate: `candidate:${"f".repeat(32)} 1 udp 1 172.17.0.3 59000 typ host` };
    const added = candidatesToAdd(
      long,
      info([
        { host: "a", port: 1, protocol: "udp" },
        { host: "b", port: 2, protocol: "udp" },
      ]),
    );
    const foundations = added.map((entry) => parseCandidateLine(entry.candidate ?? "")?.foundation ?? "");
    expect(foundations.every((foundation) => foundation.length === 32)).toBe(true);
    expect(new Set(foundations).size).toBe(2);
  });
});

describe("parseCandidateLine", () => {
  it("reads a line without the candidate: prefix", () => {
    const parsed = parseCandidateLine("1 1 udp 2130706431 10.0.0.2 5000 typ host");
    expect(parsed).toEqual({
      prefix: "",
      foundation: "1",
      component: "1",
      transport: "udp",
      priority: "2130706431",
      address: "10.0.0.2",
      port: "5000",
      rest: " typ host",
    });
  });

  it("answers null for a line that is not a candidate", () => {
    expect(parseCandidateLine("")).toBeNull();
    expect(parseCandidateLine("candidate:1 1 udp")).toBeNull();
  });
});
