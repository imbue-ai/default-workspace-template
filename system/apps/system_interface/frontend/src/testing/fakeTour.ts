/**
 * The show-around tour's test doubles: a fake of its two routes (``GET`` and ``POST /api/tour``), whose record is set
 * by hand, whose read can be made to fail, and whose every write is recorded; and a short list of cards, one of
 * each kind the tour draws.
 */

import type { TourApi, TourRecord } from "../model/tour";
import type { TourCard } from "../model/tourCards";

/** Three cards: one popped out of the avatar whose Next snaps the chat, one about a chrome control with a link, one
 *  at the launcher with a picture, bullets, and the Done label. */
export const FAKE_TOUR_CARDS: readonly TourCard[] = [
  {
    id: "a",
    text: "Hello there.",
    bullets: null,
    target: "avatar",
    chromeAnchor: null,
    image: null,
    link: null,
    action: "snap-avatar-window-right",
    buttonLabel: null,
  },
  {
    id: "b",
    text: "About sharing.",
    bullets: null,
    target: null,
    chromeAnchor: "share",
    image: null,
    link: { label: "Join us", url: "https://example.com/discord" },
    action: null,
    buttonLabel: null,
  },
  {
    id: "c",
    text: "Devices.",
    bullets: ["Laptops", "Phones"],
    target: "launcher",
    chromeAnchor: null,
    image: { url: "https://cdn.example/devices.png", alt: "devices" },
    link: null,
    action: null,
    buttonLabel: "Done",
  },
];

export class FakeTourApi implements TourApi {
  record: TourRecord;
  isFetchFailing = false;
  /** When set, a read answers only once ``resolveFetch`` is called, with the record as it was when the read began. */
  isFetchDeferred = false;
  readonly writes: TourRecord[] = [];
  private deferredFetch: (() => void) | null = null;

  constructor(options: { isSeen?: boolean; resumeIndex?: number | null } = {}) {
    this.record = { isSeen: options.isSeen ?? false, resumeIndex: options.resumeIndex ?? null };
  }

  async fetch(): Promise<TourRecord> {
    if (this.isFetchFailing) throw new Error("down");
    const snapshot = this.record;
    if (!this.isFetchDeferred) return snapshot;
    return new Promise((resolve) => {
      this.deferredFetch = () => resolve(snapshot);
    });
  }

  resolveFetch(): void {
    this.deferredFetch?.();
    this.deferredFetch = null;
  }

  async write(record: TourRecord): Promise<void> {
    this.writes.push(record);
    this.record = record;
  }
}
