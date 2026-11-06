import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { declaredParts, unmarkedDeclaredParts } from "@imbue/workspace-ui/src/testing/appParts";

const APP_TOML = join(__dirname, "../../app.toml");

describe("the parts the app declares for themes", () => {
  it("are each marked somewhere in the frontend, so a theme's rules for them can apply", () => {
    expect(declaredParts(APP_TOML).parts.length).toBeGreaterThan(0);
    expect(unmarkedDeclaredParts(APP_TOML, __dirname)).toEqual([]);
  });
});
