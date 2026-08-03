// The standalone machine settings page (/workspace/<id>/settings?group=):
// the deep-linkable full-page variant of the options panel's Machine
// settings tab, sharing the same model + group components.

import m from "mithril";
import { Icon16 } from "../components/Icon";
import { Notice } from "../components/Notice";
import { Button } from "../components/Button";
import { PageContainer } from "../components/Layout";
import { Spinner } from "../components/Spinner";
import type { SettingsGroup } from "../../models/workspaceOptions";
import { WorkspaceOptionsModel } from "../../models/workspaceOptions";
import { SettingsGroups } from "./workspace/SettingsGroups";

function requestedGroup(): SettingsGroup {
  const group = m.route.param("group");
  return group === "account" || group === "backup" ? group : "general";
}

export const WorkspaceSettingsPage: m.ClosureComponent = () => {
  let model: WorkspaceOptionsModel | null = null;
  let group: SettingsGroup = "general";

  return {
    oninit() {
      group = requestedGroup();
      model = new WorkspaceOptionsModel(m.route.param("agentId"));
      void model.load();
    },
    onremove() {
      model?.dispose();
    },
    view() {
      if (model === null) return null;
      if (model.status === "loading") {
        return m(
          PageContainer,
          m("p", { class: "type-body text-secondary flex items-center gap-2 pt-10" }, [
            m(Spinner, { size: "sm" }),
            "Loading machine settings...",
          ]),
        );
      }
      if (model.status === "load_failed" || model.data === null) {
        return m(
          PageContainer,
          m("div", { class: "pt-10 flex flex-col gap-3 items-start" }, [
            m(Notice, { variant: "warn" }, `Could not load this machine's settings: ${model.loadErrorMessage}`),
            m(Button, { variant: "secondary", onclick: () => void model?.load() }, "Try again"),
          ]),
        );
      }
      return m(PageContainer, { extra: "flex flex-col min-h-0 pt-10" }, [
        m("h1", { class: "type-heading text-primary flex items-center gap-2 min-w-0 shrink-0" }, [
          m(Icon16, { name: "settings", size: "lg", extra: "shrink-0" }),
          m("span", { class: "shrink-0" }, "Machine settings:"),
          m("span", { class: "truncate max-w-[280px]" }, model.data.name),
        ]),
        m(SettingsGroups, {
          model,
          selectedGroup: group,
          onSelectGroup: (nextGroup: SettingsGroup) => {
            group = nextGroup;
          },
        }),
      ]);
    },
  };
};
