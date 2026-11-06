/** The media path summary over a stats report built by hand, and the Details rows. */
import { describe, expect, it } from "vitest";

import { DetailsPanel, describeDetails, describePath, summarizeStats } from "./stream_details";
import type { DetailsContext, StatsEntry } from "./stream_details";

function report(options: { isRelay: boolean; protocol: string }): StatsEntry[] {
  return [
    { id: "T1", type: "transport", selectedCandidatePairId: "P1" },
    {
      id: "P0",
      type: "candidate-pair",
      nominated: false,
      state: "failed",
      localCandidateId: "L0",
      remoteCandidateId: "R0",
    },
    {
      id: "P1",
      type: "candidate-pair",
      nominated: true,
      state: "succeeded",
      localCandidateId: "L1",
      remoteCandidateId: "R1",
      currentRoundTripTime: 0.0421,
    },
    {
      id: "L1",
      type: "local-candidate",
      candidateType: options.isRelay ? "relay" : "host",
      protocol: options.isRelay ? "udp" : options.protocol,
      relayProtocol: options.isRelay ? "tcp" : undefined,
    },
    {
      id: "R1",
      type: "remote-candidate",
      candidateType: "host",
      protocol: options.protocol,
      address: "135.148.34.234",
      port: 22023,
    },
    {
      id: "V1",
      type: "inbound-rtp",
      kind: "video",
      bytesReceived: 250_000,
      timestamp: 10_000,
      frameWidth: 960,
      frameHeight: 816,
      framesPerSecond: 20,
    },
  ];
}

describe("summarizeStats", () => {
  it("names a direct pair with its protocol, address and round trip, and the picture", () => {
    const summary = summarizeStats(report({ isRelay: false, protocol: "udp" }), null);
    expect(summary.path).toBe("direct");
    expect(summary.protocol).toBe("udp");
    expect(summary.remoteAddress).toBe("135.148.34.234:22023");
    expect(summary.roundTripMs).toBe(42);
    expect(summary.frameWidth).toBe(960);
    expect(summary.frameHeight).toBe(816);
    expect(summary.framesPerSecond).toBe(20);
    expect(summary.kilobitsPerSecond).toBeNull();
    expect(summary.sample).toEqual({ bytesReceived: 250_000, timestampMs: 10_000 });
    expect(describePath(summary)).toBe("WebRTC direct over udp to 135.148.34.234:22023");
  });

  it("names a relayed pair by the relay's transport", () => {
    const summary = summarizeStats(report({ isRelay: true, protocol: "udp" }), null);
    expect(summary.path).toBe("relay");
    expect(summary.protocol).toBe("tcp");
    expect(describePath(summary)).toBe("WebRTC through a relay (TURN) over tcp to 135.148.34.234:22023");
  });

  it("computes the bitrate from the previous sample", () => {
    const summary = summarizeStats(report({ isRelay: false, protocol: "tcp" }), {
      bytesReceived: 0,
      timestampMs: 8_000,
    });
    // 250,000 bytes in two seconds: 1,000 kbit/s.
    expect(summary.kilobitsPerSecond).toBe(1000);
  });

  it("answers no path for an empty report", () => {
    const summary = summarizeStats([], null);
    expect(summary.path).toBe("none");
    expect(describePath(summary)).toBe("No media path connected");
  });
});

describe("describeDetails", () => {
  const connectInfo = {
    ice_servers: [{ urls: "turn:turn.example:3478" }],
    candidates: [
      { host: "135.148.34.234", port: 22023, protocol: "udp" },
      { host: "135.148.34.234", port: 22023, protocol: "tcp" },
    ],
    expires_at: null,
  };

  it("lists the path, the endpoints offered, the relay servers, the Screen and the picture", () => {
    const rows = describeDetails(summarizeStats(report({ isRelay: false, protocol: "udp" }), null), {
      isScreencast: false,
      videoId: "watch",
      screenSize: { width: 960, height: 816 },
      connectInfo,
      isRelayAttempt: false,
      screencastFps: 5,
    });
    expect(rows).toEqual([
      "Path: WebRTC direct over udp to 135.148.34.234:22023",
      "Endpoints offered: udp 135.148.34.234:22023, tcp 135.148.34.234:22023",
      "Relay servers: 1",
      "Screen: 960x816, profile watch",
      "Picture: 960x816, 20 fps, round trip 42 ms",
    ]);
  });

  it("says so when the picture is the screencast", () => {
    const rows = describeDetails(summarizeStats([], null), {
      isScreencast: true,
      videoId: "idle",
      screenSize: null,
      connectInfo: { ice_servers: [], candidates: [], expires_at: null },
      isRelayAttempt: true,
      screencastFps: 5,
    });
    expect(rows).toEqual([
      "Path: JPEG screencast at 5 fps (no WebRTC pair connected; retrying)",
      "Endpoints offered: none (the server's own address)",
      "Relay servers: 0 (relay attempt)",
      "Screen: unknown, profile idle",
    ]);
  });
});

describe("DetailsPanel", () => {
  const context: DetailsContext = {
    isScreencast: false,
    videoId: "watch",
    screenSize: { width: 960, height: 816 },
    connectInfo: { ice_servers: [], candidates: [], expires_at: null },
    isRelayAttempt: false,
    screencastFps: 5,
  };

  /** ``entriesOf`` reads the report through ``forEach`` alone, which a Map of the entries answers. */
  function reportOf(entries: StatsEntry[]): RTCStatsReport {
    return new Map(entries.map((entry) => [entry.id, entry])) as unknown as RTCStatsReport;
  }

  async function flush(): Promise<void> {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }

  function rows(panel: DetailsPanel): string[] {
    return Array.from(panel.element.querySelectorAll("li"), (item) => item.textContent ?? "");
  }

  it("drops a refresh still awaiting its stats when the panel was closed and reopened meanwhile", async () => {
    const pending: ((report: RTCStatsReport) => void)[] = [];
    const panel = new DetailsPanel({
      getStats: () => new Promise<RTCStatsReport>((resolve) => pending.push(resolve)),
      context: () => context,
    });
    panel.setOpen(true);
    panel.setOpen(false);
    panel.setOpen(true);
    expect(pending.length).toBe(2);
    expect(panel.element.hidden).toBe(false);
    // The closed panel's refresh answers first: nothing of it is shown.
    pending[0](reportOf(report({ isRelay: true, protocol: "udp" })));
    await flush();
    expect(rows(panel)).toEqual([]);
    pending[1](reportOf(report({ isRelay: false, protocol: "udp" })));
    await flush();
    expect(rows(panel)[0]).toBe("Path: WebRTC direct over udp to 135.148.34.234:22023");
    panel.setOpen(false);
    expect(panel.element.hidden).toBe(true);
  });
});
