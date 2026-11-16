import { afterEach, describe, expect, it } from "vitest";
import type { Editor } from "@tiptap/core";

import { acceptSuggestion, createEditor, rejectSuggestion, reviewCounts, setMode } from "./editor";
import { currentText, openText } from "./document";

let editor: Editor | null = null;

function makeEditor(): Editor {
  const element = document.createElement("div");
  document.body.appendChild(element);
  editor = createEditor(element, () => {});
  return editor;
}

afterEach(() => {
  editor?.destroy();
  editor = null;
});

const PLAN = `---
title: A plan
tags: [x]
---

# Plan for the import

Some intro text with *emphasis*, a [link](https://example.com) and \`code\`.

* star bullets stay stars
* second item

1) odd ordered list
2) second

<!-- an html comment the editor cannot show -->

| a | b |
|---|---|
| 1 | 2 |

\`\`\`python
print("hi")
\`\`\`

[ref]: https://example.com/ref

Last line without newline`;

describe("opening and saving", () => {
  it("writes an untouched file back byte for byte", () => {
    const ed = makeEditor();
    const open = openText(ed, PLAN);
    expect(open.loaded.preserving).toBe(true);
    expect(currentText(ed, open)).toBe(PLAN);
  });

  it("rewrites only the edited block", () => {
    const ed = makeEditor();
    const open = openText(ed, PLAN);
    // Put the cursor at the end of the heading and type.
    let headingEnd = 0;
    ed.state.doc.descendants((node, pos) => {
      if (node.type.name === "heading" && !headingEnd) headingEnd = pos + node.nodeSize - 1;
      return true;
    });
    setMode(ed, "edit");
    ed.chain().setTextSelection(headingEnd).insertContent(" (v2)").run();
    const text = currentText(ed, open);
    expect(text).toBe(PLAN.replace("# Plan for the import", "# Plan for the import (v2)"));
  });

  it("records typing in Suggest mode as CriticMarkup", () => {
    const ed = makeEditor();
    const open = openText(ed, "# Title\n\nKeep this sentence.\n");
    setMode(ed, "suggest");
    let end = 0;
    ed.state.doc.descendants((node, pos) => {
      if (node.type.name === "paragraph") end = pos + node.nodeSize - 1;
      return true;
    });
    ed.chain().setTextSelection(end).insertContent(" Added").run();
    expect(currentText(ed, open)).toBe("# Title\n\nKeep this sentence.{++ Added++}\n");
    expect(reviewCounts(ed).suggestions).toBe(1);
  });

  it("records deletions in Suggest mode instead of deleting", () => {
    const ed = makeEditor();
    const open = openText(ed, "Remove the word maybe here.\n");
    setMode(ed, "suggest");
    const start = 1 + "Remove the word ".length;
    ed.chain()
      .setTextSelection({ from: start, to: start + "maybe ".length })
      .deleteSelection()
      .run();
    expect(currentText(ed, open)).toBe("Remove the word {--maybe --}here.\n");
  });

  it("reads CriticMarkup from the file and can accept or reject it", () => {
    const ed = makeEditor();
    const source = "One {++two ++}three {--four --}five {~~six~>seven~~} {==eight==}{>>Sam: why?<<} end{>>note<<}\n";
    const open = openText(ed, source);
    expect(currentText(ed, open)).toBe(source);
    expect(reviewCounts(ed)).toEqual({ suggestions: 3, comments: 2 });
    acceptSuggestion(ed, null);
    expect(currentText(ed, open)).toBe("One two three five seven {==eight==}{>>Sam: why?<<} end{>>note<<}\n");
  });

  it("rejects every suggestion", () => {
    const ed = makeEditor();
    const open = openText(ed, "One {++two ++}three {--four --}five {~~six~>seven~~}\n");
    rejectSuggestion(ed, null);
    expect(currentText(ed, open)).toBe("One three four five six\n");
  });

  it("adds a comment without tracking it", () => {
    const ed = makeEditor();
    const open = openText(ed, "Ship it on Friday.\n");
    setMode(ed, "suggest");
    const from = 1 + "Ship it on ".length;
    ed.chain()
      .setTextSelection({ from, to: from + "Friday".length })
      .setMark("comment", { text: "Monday?" })
      .run();
    expect(currentText(ed, open)).toBe("Ship it on {==Friday==}{>>Monday?<<}.\n");
  });

  it("applies formatting directly in Suggest mode", () => {
    const ed = makeEditor();
    const open = openText(ed, "Make this bold.\n");
    setMode(ed, "suggest");
    ed.chain()
      .setTextSelection({ from: 1 + "Make ".length, to: 1 + "Make this".length })
      .toggleBold()
      .run();
    expect(currentText(ed, open)).toBe("Make **this** bold.\n");
  });

  it("keeps a new paragraph typed in Suggest mode as an insertion", () => {
    const ed = makeEditor();
    const open = openText(ed, "First.\n");
    setMode(ed, "suggest");
    ed.chain()
      .setTextSelection(1 + "First.".length)
      .splitBlock()
      .insertContent("Second.")
      .run();
    expect(currentText(ed, open)).toBe("First.\n\n{++Second.++}\n");
  });
});

describe("hard-wrapped paragraphs", () => {
  it("typing at the end of a wrapped paragraph suggests only the new words", () => {
    const ed = makeEditor();
    const source = "First line of a paragraph\nthat the agent wrapped\nat eighty columns.\n\nNext.\n";
    const open = openText(ed, source);
    expect(currentText(ed, open)).toBe(source);
    setMode(ed, "suggest");
    let end = 0;
    ed.state.doc.descendants((node, pos) => {
      if (node.type.name === "paragraph" && !end) end = pos + node.nodeSize - 1;
      return true;
    });
    ed.chain().setTextSelection(end).insertContent(" More").run();
    expect(currentText(ed, open)).toBe(
      "First line of a paragraph that the agent wrapped at eighty columns.{++ More++}\n\nNext.\n",
    );
  });
});

describe("block boundaries", () => {
  it("keeps a task list after a paragraph that ends in a note", () => {
    const ed = makeEditor();
    const source = "Para {>>note<<}.\n\n- [ ] a task item\n- [x] a done item\n";
    const open = openText(ed, source);
    expect(ed.getJSON().content?.[1]?.type).toBe("taskList");
    expect(currentText(ed, open)).toBe(source);
  });

  it("joins the blocks around a deleted one with one blank line", () => {
    const ed = makeEditor();
    const open = openText(ed, "# A\n\nB paragraph.\n\nC paragraph.\n");
    setMode(ed, "edit");
    let from = 0;
    let to = 0;
    ed.state.doc.forEach((node, offset, index) => {
      if (index === 1) {
        from = offset;
        to = offset + node.nodeSize;
      }
    });
    ed.chain().deleteRange({ from, to }).run();
    expect(currentText(ed, open)).toBe("# A\n\nC paragraph.\n");
  });

  it("puts a new block on its own between two file blocks", () => {
    const ed = makeEditor();
    const open = openText(ed, "One.\nTwo lines.\n\nThree.\n");
    setMode(ed, "edit");
    const first = ed.state.doc.child(0);
    ed.chain()
      .setTextSelection(first.nodeSize - 1)
      .splitBlock()
      .insertContent("New.")
      .run();
    expect(currentText(ed, open)).toBe("One.\nTwo lines.\n\nNew.\n\nThree.\n");
  });
});
