import { describe, expect, it } from "vitest";

import { WORKSPACE_COLOR, isContractEmbedderMessageType } from "./embed";

describe("the embedder messages the workspace dispatches", () => {
  it("include the workspace color, so a color picked in Imbue Studio reaches the shell", () => {
    expect(WORKSPACE_COLOR).toBe("minds:workspace-color");
    expect(isContractEmbedderMessageType(WORKSPACE_COLOR)).toBe(true);
  });
});
