// Entry point for the hosted minds web chrome. The connector serves this
// bundle's index.html for every /web path; mithril routes on the real path
// (history API) under the /web prefix.

import m from "mithril";
import "./style.css";
import { gated } from "./views/shell";
import { OverviewView } from "./views/overview";
import { CreateView } from "./views/create";
import { SettingsView } from "./views/settings";
import { WorkspaceView } from "./views/workspace";

function syncDarkMode(): void {
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  const apply = () =>
    document.documentElement.classList.toggle("dark", media.matches);
  apply();
  media.addEventListener("change", apply);
}

function main(): void {
  syncDarkMode();
  const root = document.getElementById("app");
  if (root === null) {
    throw new Error("Missing #app mount point");
  }
  m.route.prefix = "/web";
  m.route(root, "/", {
    "/": gated(OverviewView),
    "/create": gated(CreateView),
    "/settings": gated(SettingsView),
    "/workspace/:hostId": gated(
      () => WorkspaceView() as unknown as m.Component<Record<string, string>>,
    ),
  });
}

main();
