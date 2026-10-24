/**
 * The Usage widget's root (app-widgets plan section 7): the page the workspace shell keeps on the
 * desktop. Framed as a widget, it draws the mode the shell tells it in the look the workspace picked (or the parts
 * its URL's ``?variant=``, ``?size=``, ``?detail=``, ``?tray=`` and ``?toolbar=`` name), reports the size of what it
 * drew after every change, and says it has nothing to show while no usage exists yet. A pick on the Usage page in
 * this browser reaches it at once over the look's broadcast channel; one made elsewhere on its next read.
 */

import m from "mithril";
import "./style.css";

import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { WidgetMode } from "@imbue/workspace-ui/src/app_contract";

import { WidgetModel } from "./models/widget";
import { WIDGET_LOOK_CHANNEL, asWidgetLook, lookOverrides } from "./models/widgetLook";
import { Widget } from "./views/Widget";

function bootstrap(): void {
  const root = document.getElementById("app");
  if (root === null) return;
  let mode: WidgetMode = "expanded";
  let wasEmpty: boolean | null = null;
  const connection = connectToShell({
    onWidgetMode: (next) => {
      mode = next;
      // A mode follows every handshake; whether there is anything to show is said again, since a report made before
      // the handshake never reached the shell.
      wasEmpty = null;
      m.redraw();
    },
  });
  const model = new WidgetModel({
    now: () => new Date(),
    redraw: () => m.redraw(),
    openStream: (url) => new EventSource(url),
  });

  const overrides = lookOverrides(window.location.search);
  const channel = new BroadcastChannel(WIDGET_LOOK_CHANNEL);
  channel.onmessage = (event: MessageEvent) => {
    const look = asWidgetLook(event.data);
    if (look !== null) model.takeLook(look);
  };

  const reportSize = (element: Element): void => {
    const box = element.getBoundingClientRect();
    connection.widgetSize(Math.ceil(box.width), Math.ceil(box.height));
  };
  const observer = new ResizeObserver((entries) => {
    for (const entry of entries) reportSize(entry.target);
  });
  m.mount(root, {
    view: () => m(Widget, { model, mode, look: { ...model.look, ...overrides } }),
    oncreate: () => {
      const widget = document.getElementById("usage-widget");
      if (widget !== null) observer.observe(widget);
    },
    onupdate: () => {
      const widget = document.getElementById("usage-widget");
      if (widget !== null) reportSize(widget);
      if (model.isEmpty !== wasEmpty) {
        wasEmpty = model.isEmpty;
        connection.widgetHidden(model.isEmpty);
      }
    },
  });
  model.start();
}

window.addEventListener("load", bootstrap);
