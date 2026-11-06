// @vitest-environment jsdom
/**
 * A download loads in a hidden frame; the frame showing a page of the answer's own means the server sent something
 * other than the file, and a frame left on ``about:blank`` (Firefox's load for a download) is the file going to the
 * browser. jsdom does not navigate frames, so each test hands the frame the document its answer would have loaded.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { startDownload } from "./download";

function answerWith(loaded: { URL: string; contentType: string; body: { textContent: string } } | null): void {
  const frame = document.querySelector("iframe[hidden]") as HTMLIFrameElement;
  Object.defineProperty(frame, "contentDocument", { value: loaded });
  frame.dispatchEvent(new Event("load"));
}

describe("startDownload", () => {
  afterEach(() => {
    document.querySelectorAll("iframe").forEach((element) => element.remove());
  });

  it("leaves a download to the browser, saying nothing and keeping the frame", () => {
    const onFailure = vi.fn();
    startDownload(document, "/notes.txt?download", "notes.txt", onFailure);
    expect(document.querySelector("iframe[hidden]")?.getAttribute("src")).toBe("/notes.txt?download");

    answerWith({ URL: "about:blank", contentType: "text/html", body: { textContent: "" } });

    expect(onFailure).not.toHaveBeenCalled();
    expect(document.querySelector("iframe[hidden]")).not.toBeNull();
  });

  it("says why when the server answers in plain words, and drops the frame", () => {
    const onFailure = vi.fn();
    startDownload(document, "/notes.txt?download", "notes.txt", onFailure);

    answerWith({
      URL: "http://localhost/notes.txt?download",
      contentType: "text/plain",
      body: { textContent: "There's nothing at /notes.txt.\n" },
    });

    expect(onFailure).toHaveBeenCalledWith("Couldn't download notes.txt: There's nothing at /notes.txt.");
    expect(document.querySelector("iframe")).toBeNull();
  });

  it("says only that it failed when the answer is a page it cannot quote", () => {
    const onFailure = vi.fn();
    startDownload(document, "/notes.txt?download", "notes.txt", onFailure);
    answerWith({
      URL: "http://localhost/notes.txt?download",
      contentType: "text/html",
      body: { textContent: "Gone" },
    });
    startDownload(document, "/report?zip", "report", onFailure);
    answerWith(null);

    expect(onFailure.mock.calls).toEqual([["Couldn't download notes.txt."], ["Couldn't download report."]]);
  });
});
