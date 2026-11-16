// Every markdown file listed in MD_CORPUS opens and, untouched, saves back byte for byte.
import { readFileSync } from "node:fs";
import { expect, it } from "vitest";
import { createEditor } from "./editor";
import { currentText, openText } from "./document";

const list = process.env["MD_CORPUS"];
it.skipIf(!list)(
  "round-trips a corpus of real files",
  () => {
    const el = document.createElement("div");
    document.body.appendChild(el);
    const ed = createEditor(el, () => {});
    const failures: string[] = [];
    let notPreserving = 0;
    const paths = readFileSync(list as string, "utf8")
      .split("\n")
      .filter(Boolean);
    for (const path of paths) {
      const text = readFileSync(path, "utf8");
      try {
        const open = openText(ed, text);
        if (!open.loaded.preserving) notPreserving++;
        if (currentText(ed, open) !== text) failures.push(path);
      } catch (error) {
        failures.push(`${path}: ${error}`);
      }
    }
    console.log(`files ${paths.length}, not preserving ${notPreserving}, failures ${failures.length}`);
    console.log(failures.slice(0, 30).join("\n"));
    expect(failures).toEqual([]);
  },
  600000,
);
