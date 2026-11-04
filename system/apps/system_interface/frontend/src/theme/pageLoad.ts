/** Settles once the page's load event has fired: every stylesheet in `<head>`, script-inserted ones and the files a
 *  theme bundle imports included, has then loaded, so a theme's tokens can be read. */
export function afterPageLoad(): Promise<void> {
  if (document.readyState === "complete") return Promise.resolve();
  return new Promise((resolve) => window.addEventListener("load", () => resolve(), { once: true }));
}
