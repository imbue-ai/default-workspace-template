// @vitest-environment jsdom
/** Which viewers get the phone layout: the Screen is never resized from one, and a small desktop window is not one. */
import { describe, expect, it } from "vitest";

import { PHONE_MEDIA_QUERY, TOUCH_MEDIA_QUERY } from "@imbue/workspace-ui/src/device_queries";
import { isPhoneViewer } from "./mobile";

function matching(...queries: string[]): (query: string) => boolean {
  return (query) => queries.includes(query);
}

describe("isPhoneViewer", () => {
  it("is a phone-sized viewport with a finger as the primary pointer", () => {
    expect(isPhoneViewer(matching(PHONE_MEDIA_QUERY, TOUCH_MEDIA_QUERY))).toBe(true);
  });

  it("is not a small desktop window", () => {
    expect(isPhoneViewer(matching(PHONE_MEDIA_QUERY))).toBe(false);
  });

  it("is not a touchscreen with a larger viewport", () => {
    expect(isPhoneViewer(matching(TOUCH_MEDIA_QUERY))).toBe(false);
    expect(isPhoneViewer(matching())).toBe(false);
  });
});
