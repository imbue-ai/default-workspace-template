/** Start a browser download of `url`, saved as `filename`, through a throwaway link. */
export function startDownload(document: Document, url: string, filename: string): void {
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}
