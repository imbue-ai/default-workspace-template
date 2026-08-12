// Cloudflare Turnstile loader + explicit-render wrapper. The widget script is
// loaded once, lazily, and only when the tier has a site key configured.

export interface TurnstileApi {
  render(container: HTMLElement, options: { sitekey: string; callback: (token: string) => void; "error-callback"?: () => void; theme?: string }): string;
  reset(widgetId: string): void;
}

declare global {
  interface Window {
    turnstile?: TurnstileApi;
  }
}

const SCRIPT_URL = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";

let loadPromise: Promise<TurnstileApi> | null = null;

export function loadTurnstile(): Promise<TurnstileApi> {
  if (window.turnstile) return Promise.resolve(window.turnstile);
  if (loadPromise) return loadPromise;
  const promise = new Promise<TurnstileApi>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = SCRIPT_URL;
    script.async = true;
    script.onload = () => {
      if (window.turnstile) {
        resolve(window.turnstile);
      } else {
        reject(new Error("Turnstile script loaded but api is missing"));
      }
    };
    script.onerror = () => reject(new Error("Failed to load the Turnstile script"));
    document.head.appendChild(script);
  });
  loadPromise = promise;
  // A failed load must not poison later attempts: drop the cached promise so
  // the next call retries (e.g. once connectivity returns).
  promise.catch(() => {
    if (loadPromise === promise) loadPromise = null;
  });
  return promise;
}
