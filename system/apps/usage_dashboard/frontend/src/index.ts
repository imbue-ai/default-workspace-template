/**
 * The Usage page's root. Framed by the workspace shell, it reports where it is (``shell:location``) and installs
 * the element context menu, then hands the page to the dashboard model, which reads the collector through this
 * app's server and keeps the page current from its stream, and its foot to the widget picker, which keeps where the
 * workspace's widget goes and how it looks there.
 */

import m from "mithril";
import "./style.css";

import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import { createContextMenuOpener } from "@imbue/workspace-ui/src/components/contextMenuOpener";
import { installElementContextMenu } from "@imbue/workspace-ui/src/context_menu";

import { DashboardModel } from "./models/dashboard";
import { routeOf } from "./models/routing";
import { WIDGET_LOOK_CHANNEL, WidgetLookPicker } from "./models/widgetLook";
import { App } from "./views/App";

function bootstrap(): void {
  const root = document.getElementById("app");
  if (root === null) return;
  let handshake: ShellHandshake | null = null;
  const connection = connectToShell({ onHandshake: (received) => (handshake = received) });
  window.addEventListener("focus", () => connection.focused());
  installElementContextMenu({ connection, handshake: () => handshake, open: createContextMenuOpener().open });
  const route = routeOf(window.location.pathname);
  connection.location(window.location.pathname, "Usage");

  const now = (): Date => new Date();
  const model = new DashboardModel({
    route,
    now,
    redraw: () => m.redraw(),
    openStream: (url) => new EventSource(url),
  });
  const lookPicker = new WidgetLookPicker({
    redraw: () => m.redraw(),
    openChannel: () => new BroadcastChannel(WIDGET_LOOK_CHANNEL),
    placeWidget: (place) => connection.placeWidget(place),
  });
  m.mount(root, { view: () => m(App, { model, lookPicker, route, now }) });
  model.start();
  void lookPicker.load();
}

window.addEventListener("load", bootstrap);
