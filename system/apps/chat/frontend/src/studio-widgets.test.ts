import { describe, expect, it } from "vitest";
import { mayHoldStudioWidget, splitStudioWidgets } from "./studio-widgets";

describe("splitStudioWidgets", () => {
  it("leaves a message with no widget as one markdown segment, unchanged", () => {
    const source = "**How do you want to start?**\n\n- one\n- two";

    expect(splitStudioWidgets(source)).toEqual([{ kind: "markdown", text: source }]);
    expect(mayHoldStudioWidget(source)).toBe(false);
  });

  it("cuts the message at a picker's tag on a line of its own", () => {
    const source = "**What do you want to use this Studio for?**\n\n<studio_category_picker/>";

    expect(splitStudioWidgets(source)).toEqual([
      { kind: "markdown", text: "**What do you want to use this Studio for?**\n" },
      { kind: "widget", widget: "category_picker" },
    ]);
  });

  it("keeps the markdown on both sides of a tag, and every tag of a message", () => {
    const source = "Before\n<studio_connector_picker/>\nBetween\n<studio_category_picker/>\nAfter";

    expect(splitStudioWidgets(source)).toEqual([
      { kind: "markdown", text: "Before" },
      { kind: "widget", widget: "connector_picker" },
      { kind: "markdown", text: "Between" },
      { kind: "widget", widget: "category_picker" },
      { kind: "markdown", text: "After" },
    ]);
  });

  it.each([
    "<studio_connector_picker />",
    "  <studio_connector_picker/>  ",
    "<studio_connector_picker></studio_connector_picker>",
  ])("takes the tag as %j for the widget", (line) => {
    expect(splitStudioWidgets(`Pick:\n${line}`)).toEqual([
      { kind: "markdown", text: "Pick:" },
      { kind: "widget", widget: "connector_picker" },
    ]);
  });

  it("leaves a tag inside a line of text where it is", () => {
    const source = "Write <studio_category_picker/> on a line of its own.";

    expect(splitStudioWidgets(source)).toEqual([{ kind: "markdown", text: source }]);
  });

  it("leaves a tag inside a code fence where it is, and takes one after the fence closes", () => {
    const fenced = "The tag:\n\n```\n<studio_category_picker/>\n```";

    expect(splitStudioWidgets(fenced)).toEqual([{ kind: "markdown", text: fenced }]);
    expect(splitStudioWidgets(`${fenced}\n<studio_category_picker/>`)).toEqual([
      { kind: "markdown", text: fenced },
      { kind: "widget", widget: "category_picker" },
    ]);
  });

  it("does not close a fence on a shorter or different one", () => {
    const source = "````\n```\n<studio_category_picker/>\n~~~~\n<studio_connector_picker/>\n````";

    expect(splitStudioWidgets(source)).toEqual([{ kind: "markdown", text: source }]);
  });

  it("leaves any other tag, and a picker tag indented as code, to the markdown", () => {
    const source = "<studio_other_picker/>\n    <studio_category_picker/>";

    expect(splitStudioWidgets(source)).toEqual([{ kind: "markdown", text: source }]);
  });

  it("draws nothing for the blank lines around a tag that is the whole message", () => {
    expect(splitStudioWidgets("\n<studio_connector_picker/>\n\n")).toEqual([
      { kind: "widget", widget: "connector_picker" },
    ]);
  });
});
