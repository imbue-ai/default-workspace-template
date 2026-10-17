import { describe, expect, it } from "vitest";
import { FakeScheduler } from "./testing/fakeScheduler";
import { ViewerVisibility } from "./visibility";

function build(hideDelayMs = 5000) {
  const scheduler = new FakeScheduler();
  const events: string[] = [];
  const visibility = new ViewerVisibility(
    { onRelease: () => events.push("release"), onRequest: () => events.push("request") },
    scheduler,
    hideDelayMs,
  );
  return { scheduler, events, visibility };
}

describe("ViewerVisibility", () => {
  it("requests media on the first visible evaluation and once only", () => {
    const { events, visibility } = build();
    visibility.evaluate();
    visibility.evaluate();
    visibility.setIntersecting(true);
    expect(events).toEqual(["request"]);
    expect(visibility.wantsMedia).toBe(true);
  });

  it("releases only after the hide delay has fully passed", () => {
    const { scheduler, events, visibility } = build();
    visibility.evaluate();
    visibility.setDocumentHidden(true);
    expect(visibility.isVisible).toBe(false);
    scheduler.advance(4999);
    expect(events).toEqual(["request"]);
    scheduler.advance(1);
    expect(events).toEqual(["request", "release"]);
    expect(visibility.wantsMedia).toBe(false);
  });

  it("a return to visibility within the delay cancels the release and requests nothing new", () => {
    const { scheduler, events, visibility } = build();
    visibility.evaluate();
    visibility.setShellShown(false);
    scheduler.advance(3000);
    visibility.setShellShown(true);
    scheduler.advance(10000);
    expect(events).toEqual(["request"]);
  });

  it("requests again when visible after a release", () => {
    const { scheduler, events, visibility } = build();
    visibility.evaluate();
    visibility.setIntersecting(false);
    scheduler.advance(5000);
    visibility.setIntersecting(true);
    expect(events).toEqual(["request", "release", "request"]);
  });

  it("every input must hold for visibility: document, shell and intersection", () => {
    const { scheduler, events, visibility } = build();
    visibility.evaluate();
    visibility.setDocumentHidden(true);
    visibility.setShellShown(false);
    visibility.setIntersecting(false);
    visibility.setDocumentHidden(false);
    scheduler.advance(5000);
    expect(events).toEqual(["request", "release"]);
    visibility.setShellShown(true);
    expect(visibility.isVisible).toBe(false);
    visibility.setIntersecting(true);
    expect(visibility.isVisible).toBe(true);
    expect(events).toEqual(["request", "release", "request"]);
  });

  it("the belt's evaluate does not restart a running hide timer", () => {
    const { scheduler, events, visibility } = build();
    visibility.evaluate();
    visibility.setDocumentHidden(true);
    scheduler.advance(3000);
    visibility.evaluate();
    scheduler.advance(2000);
    expect(events).toEqual(["request", "release"]);
  });

  it("reset forgets the media so the next visible evaluation requests it", () => {
    const { events, visibility } = build();
    visibility.evaluate();
    visibility.reset();
    visibility.evaluate();
    expect(events).toEqual(["request", "request"]);
  });
});
