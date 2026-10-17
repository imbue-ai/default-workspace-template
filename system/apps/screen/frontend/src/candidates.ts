/** Candidate rewriting (docs/system/specs/screen/spec.md section 5.3): neko advertises its container-internal
 *  address on the ICE mux port, which the viewer cannot reach; ``connect-info`` says where that port is reachable
 *  from here (a published host port, a relay's tcp proxy), so every server candidate becomes one copy per matching
 *  endpoint with the address and port replaced and a foundation of its own, and the original is dropped. With no
 *  endpoints known the server's candidate is used as given. */

import type { ConnectInfo, EndpointCandidate } from "./state";

const MAX_FOUNDATION_LENGTH = 32;

export interface ParsedCandidate {
  prefix: string;
  foundation: string;
  component: string;
  transport: string;
  priority: string;
  address: string;
  port: string;
  rest: string;
}

/** The fields of a ``candidate:`` line (RFC 8839 section 5.1), or null for a line that is not one (an
 *  end-of-candidates marker is the empty string). */
export function parseCandidateLine(line: string): ParsedCandidate | null {
  const match = /^(candidate:)?(\S+) (\S+) (\S+) (\S+) (\S+) (\S+)(.*)$/.exec(line.trim());
  if (match === null) return null;
  return {
    prefix: match[1] ?? "",
    foundation: match[2],
    component: match[3],
    transport: match[4],
    priority: match[5],
    address: match[6],
    port: match[7],
    rest: match[8],
  };
}

function distinctFoundation(foundation: string, index: number): string {
  const suffix = String(index);
  return foundation.slice(0, MAX_FOUNDATION_LENGTH - suffix.length) + suffix;
}

/** The candidate with its address and port replaced by the endpoint's and a foundation distinct per copy; everything
 *  else (component, transport, priority, type, tcptype, the SDP mid and line index) is kept. */
export function rewriteCandidate(
  original: RTCIceCandidateInit,
  parsed: ParsedCandidate,
  endpoint: EndpointCandidate,
  index: number,
): RTCIceCandidateInit {
  const line =
    `${parsed.prefix}${distinctFoundation(parsed.foundation, index)} ${parsed.component} ${parsed.transport} ` +
    `${parsed.priority} ${endpoint.host} ${endpoint.port}${parsed.rest}`;
  return { ...original, candidate: line };
}

export function candidatesToAdd(candidate: RTCIceCandidateInit, connectInfo: ConnectInfo): RTCIceCandidateInit[] {
  if (connectInfo.candidates.length === 0) return [candidate];
  const parsed = parseCandidateLine(candidate.candidate ?? "");
  if (parsed === null) return [candidate];
  const transport = parsed.transport.toLowerCase();
  return connectInfo.candidates
    .filter((endpoint) => endpoint.protocol.toLowerCase() === transport)
    .map((endpoint, index) => rewriteCandidate(candidate, parsed, endpoint, index));
}
