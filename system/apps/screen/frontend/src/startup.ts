/** What the viewer asks the service for at page load, all at once (docs/system/specs/screen/spec.md section 11): the
 *  state, a neko session and ``connect-info``. None waits on another: a session asked for before the stack runs is
 *  refused and asked for again once the state says it runs, and ``connect-info`` depends on nothing but whether this
 *  client needed a relay last time. */

import { pageStorage, readNeedsRelay } from "./relay_memory";
import { fetchConnectInfo, fetchState, postSession } from "./service_client";
import type { NekoSessionAnswer, ScreenStateDocument } from "./state";
import type { PrefetchedConnectInfo } from "./webrtc";

export interface StartupFetches {
  readonly state: Promise<ScreenStateDocument>;
  /** Taken by the first sign-in; a refusal there signs in afresh. */
  readonly session: Promise<NekoSessionAnswer>;
  /** Taken by the first stream's first request. */
  readonly connectInfo: PrefetchedConnectInfo;
}

export function startStartupFetches(now: () => number): StartupFetches {
  const isRelay = readNeedsRelay(pageStorage());
  const session = postSession();
  const answer = fetchConnectInfo(isRelay);
  // Each one's taker handles its failure, and one may never be taken (a session refused before the stack runs, an
  // answer grown too old): until then a failure is not an unhandled one.
  session.catch(() => undefined);
  answer.catch(() => undefined);
  return { state: fetchState(), session, connectInfo: { isRelay, answer, askedAt: now() } };
}
