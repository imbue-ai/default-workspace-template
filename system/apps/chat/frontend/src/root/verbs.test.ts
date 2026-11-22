// @vitest-environment jsdom
/** The rename verb: a name another chat has is told apart from any other refusal by the 409 body's kind. */
import { afterEach, describe, expect, it, vi } from "vitest";
import { HttpError } from "@imbue/workspace-ui/src/models/http";
import { NameTakenError, renameChat } from "./verbs";

afterEach(() => {
  vi.unstubAllGlobals();
});

function answerWith(status: number, body: unknown): void {
  vi.stubGlobal("fetch", () => Promise.resolve(new Response(JSON.stringify(body), { status })));
}

async function renameError(): Promise<unknown> {
  return renameChat("agent-first", "Trip plans").then(
    () => null,
    (error: unknown) => error,
  );
}

describe("renameChat", () => {
  it("answers NameTakenError with the chat app's sentence for a name another chat has", async () => {
    answerWith(409, { detail: "A chat named 'Trip plans' already exists; pick another name", kind: "name_taken" });

    const error = await renameError();

    expect(error).toBeInstanceOf(NameTakenError);
    expect((error as NameTakenError).message).toBe("A chat named 'Trip plans' already exists; pick another name");
  });

  it("answers a plain HttpError with the detail for any other refusal", async () => {
    answerWith(409, { detail: "the chat is moving to another agent" });

    const error = await renameError();

    expect(error).toBeInstanceOf(HttpError);
    expect(error).not.toBeInstanceOf(NameTakenError);
    expect((error as HttpError).status).toBe(409);
    expect((error as HttpError).message).toBe("the chat is moving to another agent");
  });
});
