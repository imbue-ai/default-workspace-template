import { describe, expect, it } from "vitest";
import { ResizeReporter } from "./resize_reporter";
import type { StageSize } from "./resize_reporter";
import { FakeScheduler } from "./testing/fakeScheduler";

function build() {
  const scheduler = new FakeScheduler();
  const sent: StageSize[] = [];
  const reporter = new ResizeReporter((size) => sent.push(size), scheduler, 300);
  return { scheduler, sent, reporter };
}

describe("ResizeReporter", () => {
  it("sends the last size of a burst once the debounce window passes", () => {
    const { scheduler, sent, reporter } = build();
    reporter.setEnabled(true);
    reporter.observe(1000, 700);
    scheduler.advance(200);
    reporter.observe(1100, 720);
    scheduler.advance(200);
    reporter.observe(1200.4, 740.6);
    scheduler.advance(299);
    expect(sent).toEqual([]);
    scheduler.advance(1);
    expect(sent).toEqual([{ width: 1200, height: 741 }]);
  });

  it("does not repeat an unchanged size, and forget makes it go out again", () => {
    const { scheduler, sent, reporter } = build();
    reporter.setEnabled(true);
    reporter.observe(800, 600);
    scheduler.advance(300);
    reporter.observe(800, 600);
    scheduler.advance(300);
    expect(sent).toHaveLength(1);
    reporter.forget();
    reporter.observe(800, 600);
    scheduler.advance(300);
    expect(sent).toHaveLength(2);
  });

  it("holds a size observed while disabled and sends it when enabled", () => {
    const { scheduler, sent, reporter } = build();
    reporter.observe(900, 500);
    scheduler.advance(1000);
    expect(sent).toEqual([]);
    reporter.setEnabled(true);
    scheduler.advance(300);
    expect(sent).toEqual([{ width: 900, height: 500 }]);
  });

  it("disabling cancels a pending send", () => {
    const { scheduler, sent, reporter } = build();
    reporter.setEnabled(true);
    reporter.observe(900, 500);
    reporter.setEnabled(false);
    scheduler.advance(1000);
    expect(sent).toEqual([]);
  });

  it("ignores a collapsed stage", () => {
    const { scheduler, sent, reporter } = build();
    reporter.setEnabled(true);
    reporter.observe(0, 0);
    reporter.observe(40, 700);
    scheduler.advance(300);
    expect(sent).toEqual([]);
  });
});
