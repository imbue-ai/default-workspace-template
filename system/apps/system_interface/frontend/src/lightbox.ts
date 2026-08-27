/**
 * A full-screen image viewer ("lightbox") opened by clicking an inline chat
 * image. It shows the image enlarged over a dimmed backdrop with a header:
 * the image's title (alt text, or the filename) centered, and Download and
 * close (X) icon buttons at the top right. It closes on a backdrop click, the X
 * button, or the Escape key.
 *
 * This is imperative (plain DOM) rather than a mithril component because chat
 * messages are rendered via `innerHTML` (see markdown.ts), so the images it
 * applies to are not part of mithril's vnode tree.
 */

import { attachHoverTooltip } from "./views/hoverTooltip";
import type { HoverTooltip } from "./views/hoverTooltip";
import { icon } from "./views/icons";
import { buttonClass } from "./views/Button";

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
// The header buttons' tooltips, disposed with the overlay so a bubble that is
// up when the lightbox closes (via Escape, say) goes with it.
let activeTooltips: HoverTooltip[] = [];

// Native `title` is not used anywhere in the workspace -- see views/hoverTooltip.ts.
function addTooltip(element: HTMLElement, label: string): void {
  const tooltip = attachHoverTooltip(element);
  tooltip.setText(label);
  activeTooltips.push(tooltip);
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") {
    closeImageLightbox();
  }
}

// Close when a backdrop element (the overlay padding or the empty area around
// the image) is clicked directly -- not when a child (image, header, button) is.
function onBackdropClick(event: MouseEvent): void {
  if (event.target === event.currentTarget) {
    closeImageLightbox();
  }
}

export function closeImageLightbox(): void {
  if (activeOverlay === null) {
    return;
  }
  activeOverlay.remove();
  activeOverlay = null;
  activeTooltips.forEach((tooltip) => tooltip.dispose());
  activeTooltips = [];
  document.removeEventListener("keydown", onKeydown);
}

export function openImageLightbox(imageUrl: string, altText: string): void {
  // Only one lightbox open at a time.
  closeImageLightbox();

  const filename = filenameFromUrl(imageUrl);

  const overlay = document.createElement("div");
  overlay.className = "image-lightbox-overlay fixed inset-0 z-(--z-overlay) flex flex-col bg-black/85";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.addEventListener("click", onBackdropClick);

  // Top bar: title centered, action icons pinned to the top right.
  const header = document.createElement("div");
  header.className = "image-lightbox-header relative flex min-h-[44px] items-center justify-center px-4 py-2.5";

  const title = document.createElement("div");
  title.className =
    "image-lightbox-title max-w-[60%] truncate text-center text-(length:--font-size-body) font-medium text-white/90";
  title.textContent = altText || filename;

  const actions = document.createElement("div");
  actions.className = "image-lightbox-actions absolute top-1/2 right-3 flex -translate-y-1/2 gap-1";

  const downloadLink = document.createElement("a");
  downloadLink.className = buttonClass("ghost-inverse", { icon: true, extra: "image-lightbox-iconbtn no-underline" });
  downloadLink.href = imageUrl;
  downloadLink.download = filename;
  // Same-origin images download in place; a cross-origin (public-URL) image the
  // browser refuses to download opens in a new tab rather than navigating away.
  downloadLink.target = "_blank";
  downloadLink.rel = "noopener noreferrer";
  downloadLink.setAttribute("aria-label", "Download image");
  downloadLink.innerHTML = icon("download", { size: 20 });

  const closeButton = document.createElement("button");
  closeButton.className = buttonClass("ghost-inverse", { icon: true, extra: "image-lightbox-iconbtn" });
  closeButton.type = "button";
  closeButton.setAttribute("aria-label", "Close image viewer");
  closeButton.innerHTML = icon("close", { size: 20 });
  closeButton.addEventListener("click", closeImageLightbox);

  addTooltip(downloadLink, "Download");
  addTooltip(closeButton, "Close");

  actions.appendChild(downloadLink);
  actions.appendChild(closeButton);
  header.appendChild(title);
  header.appendChild(actions);

  const body = document.createElement("div");
  body.className = "image-lightbox-body flex min-h-0 flex-1 items-center justify-center px-8 pb-8";
  body.addEventListener("click", onBackdropClick);

  const image = document.createElement("img");
  image.className = "image-lightbox-img max-h-full max-w-full cursor-default rounded-md object-contain shadow-overlay";
  image.src = imageUrl;
  image.alt = altText;
  body.appendChild(image);

  overlay.appendChild(header);
  overlay.appendChild(body);

  document.body.appendChild(overlay);
  activeOverlay = overlay;
  document.addEventListener("keydown", onKeydown);
}
