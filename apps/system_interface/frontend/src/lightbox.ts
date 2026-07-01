/**
 * A full-screen image viewer ("lightbox") opened by clicking an inline chat
 * image. It shows the image enlarged over a dimmed backdrop, with a Download
 * button and close affordances (click the backdrop, the Close button, or press
 * Escape).
 *
 * This is imperative (plain DOM) rather than a mithril component because chat
 * messages are rendered via `innerHTML` (see markdown.ts), so the images it
 * applies to are not part of mithril's vnode tree.
 */

function filenameFromUrl(imageUrl: string): string {
  try {
    const path = new URL(imageUrl, window.location.href).pathname;
    const basename = path.split("/").pop() ?? "";
    return decodeURIComponent(basename) || "image";
  } catch {
    return "image";
  }
}

let activeOverlay: HTMLElement | null = null;

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") {
    closeImageLightbox();
  }
}

export function closeImageLightbox(): void {
  if (activeOverlay === null) {
    return;
  }
  activeOverlay.remove();
  activeOverlay = null;
  document.removeEventListener("keydown", onKeydown);
}

export function openImageLightbox(imageUrl: string, altText: string): void {
  // Only one lightbox open at a time.
  closeImageLightbox();

  const overlay = document.createElement("div");
  overlay.className = "image-lightbox-overlay";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.addEventListener("click", (event) => {
    // Close only when the dimmed backdrop itself is clicked -- not the image
    // or the toolbar sitting on top of it.
    if (event.target === overlay) {
      closeImageLightbox();
    }
  });

  const toolbar = document.createElement("div");
  toolbar.className = "image-lightbox-toolbar";

  const downloadLink = document.createElement("a");
  downloadLink.className = "image-lightbox-btn";
  downloadLink.href = imageUrl;
  downloadLink.download = filenameFromUrl(imageUrl);
  // Same-origin images download in place; a cross-origin (public-URL) image the
  // browser refuses to download opens in a new tab rather than navigating away
  // from the chat.
  downloadLink.target = "_blank";
  downloadLink.rel = "noopener noreferrer";
  downloadLink.textContent = "Download";

  const closeButton = document.createElement("button");
  closeButton.className = "image-lightbox-btn";
  closeButton.type = "button";
  closeButton.textContent = "Close";
  closeButton.setAttribute("aria-label", "Close image viewer");
  closeButton.addEventListener("click", closeImageLightbox);

  toolbar.appendChild(downloadLink);
  toolbar.appendChild(closeButton);

  const image = document.createElement("img");
  image.className = "image-lightbox-img";
  image.src = imageUrl;
  image.alt = altText;

  overlay.appendChild(toolbar);
  overlay.appendChild(image);

  document.body.appendChild(overlay);
  activeOverlay = overlay;
  document.addEventListener("keydown", onKeydown);
}
