import { describe, expect, it } from "vitest";

import { WORKSPACE_COLOR, isContractEmbedderMessageType } from "@imbue/workspace-ui/src/embed";

describe("the embedder messages the shell dispatches", () => {
  it("include the workspace color, so a color picked in Imbue Studio reaches the shell", () => {
    expect(WORKSPACE_COLOR).toBe("minds:workspace-color");
    expect(isContractEmbedderMessageType(WORKSPACE_COLOR)).toBe(true);
  });
});
