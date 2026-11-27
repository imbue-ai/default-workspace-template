// The cover over a first-run workspace's desktop: the chat app's "Your own cloud computer, any AI" page,
// framed over everything until the owner connects a provider or skips. The page is the chat app's because the
// sign-ins are; the shell only gives it the whole window, above the windows, the taskbar and the tour card.
// It is no window of the desktop, so the shell sends it no handshake: what the Imbue Studio chrome can do, which
// the page's sign-ins go by, it hears as the chrome's own message, repeated to it once it has loaded.

import m from "mithril";

import { PAGE_FRAME_ALLOW, PAGE_FRAME_SANDBOX } from "../pages/livePages";
import { repeatEmbedderCapabilitiesTo } from "../relay";

export const FIRST_RUN_COVER_ID = "first-run-cover";

export function firstRunCoverView(url: string): m.Children {
  return m("iframe", {
    id: FIRST_RUN_COVER_ID,
    title: "Connect an AI provider",
    src: url,
    sandbox: PAGE_FRAME_SANDBOX,
    allow: PAGE_FRAME_ALLOW,
    class: "fixed inset-0 z-(--z-tooltip) h-full w-full border-0",
    onload: (event: Event) => repeatEmbedderCapabilitiesTo(event.target as HTMLIFrameElement),
  });
}
