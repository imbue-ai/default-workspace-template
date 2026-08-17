// ApiError's message ends up in user-facing error banners, so a structured
// refusal's human-readable `message` must surface as prose, with the raw
// JSON blob only as the fallback for unstructured details.

import { describe, expect, it } from "vitest";
import { ApiError } from "./api";

describe("ApiError", () => {
  it("surfaces a structured detail's message as prose", () => {
    const error = new ApiError(403, {
      code: "email_not_verified",
      email: "alice@example.com",
      sent: true,
      message: "Creating a remote workspace requires a verified email address.",
    });
    expect(error.message).toBe("API error 403: Creating a remote workspace requires a verified email address.");
  });

  it("uses a plain string detail directly", () => {
    const error = new ApiError(503, "SuperTokens not configured on the server");
    expect(error.message).toBe("API error 503: SuperTokens not configured on the server");
  });

  it("falls back to the JSON blob when the detail has no string message", () => {
    const error = new ApiError(422, { fields: ["host_name"] });
    expect(error.message).toBe('API error 422: {"fields":["host_name"]}');
  });

  it("falls back to the JSON blob when the structured message is empty", () => {
    const error = new ApiError(403, { code: "quota_exceeded", message: "" });
    expect(error.message).toBe('API error 403: {"code":"quota_exceeded","message":""}');
  });

  it("keeps the status and detail readable as public fields", () => {
    const detail = { code: "quota_exceeded", message: "Quota exceeded" };
    const error = new ApiError(403, detail);
    expect(error.status).toBe(403);
    expect(error.detail).toBe(detail);
  });
});
