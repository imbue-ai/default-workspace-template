// Small shared view helpers. The class recipes are copied from the minds
// SPA's component constants (apps/minds/frontend/src/views/components) so
// controls here render like the app's.

import m from "mithril";

export const BTN_BASE =
  "inline-flex items-center justify-center gap-1.5 leading-tight " +
  "transition-transform duration-100 ease-in-out disabled:opacity-40 disabled:cursor-not-allowed " +
  "cursor-pointer no-underline whitespace-nowrap active:scale-[0.98] " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent";

export const BTN_PRIMARY =
  BTN_BASE + " px-4 py-3 rounded-md type-label w-full active:!scale-[0.99] " +
  "bg-surface-inverse text-inverse-primary border border-transparent hover:opacity-80";

export const BTN_SECONDARY =
  BTN_BASE + " px-4 py-3 rounded-md type-label w-full active:!scale-[0.99] " +
  "bg-transparent text-primary border border-default hover:bg-fill-hover";

export const BTN_GHOST_SM =
  BTN_BASE + " px-4 py-2 rounded-md type-label bg-transparent text-primary border border-transparent hover:bg-fill-hover";

export const INPUT_CLASS =
  "w-full rounded-md p-2 type-body border border-strong bg-surface-primary text-primary " +
  "placeholder:text-tertiary hover:border-stronger focus:border-stronger " +
  "focus:outline-2 focus:outline-offset-2 focus:outline-accent";

export const LINK_CLASS = "text-accent hover:underline cursor-pointer";

// The Minds wordmark (same path data the OAuth success page carries), scaled
// down for the card header. Fills with currentColor so it themes.
export function MindsWordmark(): m.Vnode {
  return m(
    "svg",
    { width: "106", height: "29", viewBox: "0 0 159 43", fill: "none", "aria-label": "Minds" },
    [
      m("path", {
        d: "M0 42V13.08H4.68V16.98C5.7 13.86 8.04 12.12 10.86 12.12C13.5 12.12 15.78 13.74 16.68 17.4C17.94 14.22 20.16 12.12 23.7 12.12C28.02 12.12 30.36 15.6 30.36 22.14V42H25.68V22.74C25.68 18.66 24.84 16.2 22.02 16.2C18.66 16.2 17.52 19.86 17.52 23.7V42H12.84V23.1C12.84 18.84 11.88 16.2 9 16.2C5.76 16.2 4.68 19.92 4.68 23.94V42H0Z",
        fill: "currentColor",
      }),
      m("path", {
        d: "M34.8366 42V37.74H48.6366V17.34H37.2966V13.08H53.7366V37.74H65.6166V42H34.8366ZM47.3766 7.98V1.08H53.9166V7.98H47.3766Z",
        fill: "currentColor",
      }),
      m("path", {
        d: "M70.3331 42V13.08H75.4931V16.98C76.9931 14.46 80.4731 12.12 84.7931 12.12C91.7531 12.12 95.7731 16.62 95.7731 24.06V42H90.6131V24.72C90.6131 19.26 88.8131 16.2 83.8931 16.2C78.4931 16.2 75.4931 20.22 75.4931 24.84V42H70.3331Z",
        fill: "currentColor",
      }),
      m("path", {
        d: "M114.59 42.9C107.03 42.9 101.21 37.38 101.21 27.54C101.21 18.78 106.49 12.12 114.65 12.12C119.51 12.12 122.69 14.76 123.95 16.98V0H129.11V42H123.95V37.98C122.39 40.68 118.91 42.9 114.59 42.9ZM115.43 38.88C120.65 38.88 124.31 34.44 124.31 27.48C124.31 20.58 120.71 16.2 115.43 16.2C110.27 16.2 106.61 20.76 106.61 27.54C106.61 34.32 110.21 38.88 115.43 38.88Z",
        fill: "currentColor",
      }),
      m("path", {
        d: "M146.846 42.9C139.046 42.9 134.546 38.64 134.426 32.46H139.466C139.646 36.36 142.286 38.88 146.906 38.88C150.866 38.88 153.566 37.08 153.566 34.14C153.566 31.86 152.006 30.36 148.526 29.7L144.146 28.86C138.746 27.84 135.326 25.08 135.326 20.64C135.326 15.72 140.006 12.12 146.546 12.12C153.506 12.12 157.706 15.54 158.066 21.42H152.966C152.546 17.94 150.086 16.2 146.186 16.2C142.706 16.2 140.486 17.88 140.486 20.34C140.486 22.68 142.166 23.82 145.406 24.42L149.906 25.26C155.126 26.22 158.666 28.8 158.666 33.6C158.666 38.76 154.226 42.9 146.846 42.9Z",
        fill: "currentColor",
      }),
    ],
  );
}

export function ErrorBanner(message: string): m.Vnode | null {
  if (!message) return null;
  return m(
    "div",
    { class: "rounded-md border border-important/40 bg-important-surface text-important type-body px-3 py-2 mb-4" },
    message,
  );
}

export function SuccessNote(message: string): m.Vnode | null {
  if (!message) return null;
  return m(
    "div",
    { class: "rounded-md border border-success/40 bg-success-surface text-success type-body px-3 py-2 mb-4" },
    message,
  );
}

export function CenteredCard(...children: m.Children[]): m.Vnode {
  return m(
    "div",
    { class: "min-h-full flex items-center justify-center px-4 py-12" },
    m(
      "div",
      { class: "w-full max-w-sm rounded-xl border border-subtle bg-surface-primary shadow-overlay p-8" },
      children,
    ),
  );
}

export function Spinner(): m.Vnode {
  return m("span", { class: "spinner inline-block align-middle", "aria-label": "Loading" });
}

/** The standard multicolor Google "G" mark (per Google's sign-in branding),
 * inlined so the button reads as a real Google sign-in affordance. */
export function GoogleLogo(): m.Vnode {
  return m(
    "svg",
    { width: "18", height: "18", viewBox: "0 0 48 48", "aria-hidden": "true" },
    [
      m("path", {
        fill: "#EA4335",
        d: "M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z",
      }),
      m("path", {
        fill: "#4285F4",
        d: "M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z",
      }),
      m("path", {
        fill: "#FBBC05",
        d: "M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z",
      }),
      m("path", {
        fill: "#34A853",
        d: "M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z",
      }),
    ],
  );
}
