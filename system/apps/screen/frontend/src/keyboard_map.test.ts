import { describe, expect, it } from "vitest";
import { keyboardMapForLanguage } from "./keyboard_map";

describe("keyboardMapForLanguage", () => {
  it("defaults to us", () => {
    expect(keyboardMapForLanguage("en-US")).toEqual({ layout: "us", variant: "" });
    expect(keyboardMapForLanguage("")).toEqual({ layout: "us", variant: "" });
    expect(keyboardMapForLanguage("xx-YY")).toEqual({ layout: "us", variant: "" });
  });

  it("maps a language to its X layout and a regional spelling to its own layout", () => {
    expect(keyboardMapForLanguage("de-DE")).toEqual({ layout: "de", variant: "" });
    expect(keyboardMapForLanguage("en-GB")).toEqual({ layout: "gb", variant: "" });
    expect(keyboardMapForLanguage("pt-BR")).toEqual({ layout: "br", variant: "" });
    expect(keyboardMapForLanguage("sv")).toEqual({ layout: "se", variant: "" });
  });
});
