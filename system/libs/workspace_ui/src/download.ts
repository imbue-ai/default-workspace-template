/**
 * Start a browser download of ``url`` (on the page's own origin) by loading it in a hidden frame; the server's
 * ``Content-Disposition: attachment`` names the file. If the server answers with something other than a file,
 * ``onFailure`` gets a note saying ``name`` could not be downloaded, with the server's words when it answered in
 * plain text.
 *
 * Not a link with a ``download`` attribute: Imbue Studio frames the workspace from another site, which partitions the
 * workspace's session cookie, and the browser sends a ``download`` link's request without it; a frame's load carries
 * it. A frame whose load shows a page of the answer's own got an answer other than the file. A download leaves the
 * frame on ``about:blank``: Chromium and WebKit fire no load for it, but Firefox does.
 */
export function startDownload(document: Document, url: string, name: string, onFailure: (note: string) => void): void {
  const frame = document.createElement("iframe");
  frame.hidden = true;
  frame.addEventListener("load", () => {
    const answer = frame.contentDocument;
    if (answer?.URL === "about:blank") {
      return;
    }
    const words = answer?.contentType === "text/plain" ? (answer.body?.textContent ?? "").trim() : "";
    frame.remove();
    onFailure(words === "" ? `Couldn't download ${name}.` : `Couldn't download ${name}: ${words}`);
  });
  frame.src = url;
  // Left in place on success: the page cannot tell when the response arrives, and removing the frame before then
  // cancels the request.
  document.body.appendChild(frame);
}
