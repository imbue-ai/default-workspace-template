// The shell's service worker (README, "Notifications on a phone"). It shows each pushed notification and, on a tap,
// opens the page of the workspace the notification names. It handles no fetches, so the pages it controls load
// exactly as they would without it.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

// The shell's root with no trailing slash: the worker is registered from it, so its scope is the root.
function shellRoot() {
  return self.registration.scope.replace(/\/$/, "");
}

// The page a notification opens: a path under the shell's root, kept on this origin whatever the payload says.
function targetUrl(path) {
  const target = new URL(shellRoot() + (typeof path === "string" && path.startsWith("/") ? path : "/"));
  return target.origin === self.location.origin ? target.href : `${shellRoot()}/`;
}

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : "" };
  }
  // Every push must show a notification: a browser that sees pushes handled silently (Safari among them) withdraws
  // the permission.
  const options = {
    body: typeof data.body === "string" ? data.body : "",
    icon: `${shellRoot()}/apple-touch-icon.png`,
    data: { url: targetUrl(data.path) },
  };
  // A notification replacing one with the same tag alerts again only with renotify, which requires a tag.
  if (typeof data.tag === "string" && data.tag) {
    options.tag = data.tag;
    options.renotify = true;
  }
  event.waitUntil(self.registration.showNotification(data.title || "Your workspace", options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || `${shellRoot()}/`;
  event.waitUntil(
    (async () => {
      const pages = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      // Navigated before it is focused: a tap lets the worker focus or open a window once, so a focus spent on a page
      // that then cannot be navigated would leave the new window below refused.
      for (const page of pages) {
        if (!("navigate" in page)) continue;
        let navigated;
        try {
          navigated = await page.navigate(url);
        } catch {
          // A page this worker does not control cannot be navigated; a new window below is the fallback.
          continue;
        }
        await (navigated || page).focus();
        return;
      }
      await self.clients.openWindow(url);
    })(),
  );
});

// A push service that rotates a subscription hands the worker a new one; registering it keeps the device on the list.
// Best effort: with no signed-in session at hand the registration is refused, and the user turns notifications on
// again from the page.
self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil(
    (async () => {
      const options = event.oldSubscription ? event.oldSubscription.options : null;
      if (!options || !options.applicationServerKey) return;
      const subscription = event.newSubscription || (await self.registration.pushManager.subscribe(options));
      const response = await fetch(`${shellRoot()}/api/web-push/devices`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ subscription: subscription.toJSON(), label: "Renewed subscription" }),
      });
      if (!response.ok) throw new Error(`the workspace answered ${response.status}`);
    })().catch((error) => console.warn("Could not register the renewed push subscription", error)),
  );
});
