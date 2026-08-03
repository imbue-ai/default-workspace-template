// The workspace options page (/workspace/<id>/options?tab=&group=&target=):
// Share machine + Machine settings tabs over one options-data load. The
// titlebar's ws-tab buttons land here; ?tab preselects the pane, ?group the
// settings group, ?target the share target.

import m from "mithril";
import { PageContainer } from "../components/Layout";
import type { OptionsTab, SettingsGroup } from "../../models/workspaceOptions";
import { WorkspaceOptionsModel } from "../../models/workspaceOptions";
import { OptionsPanel } from "./workspace/OptionsPanel";

function requestedTab(): OptionsTab {
  return m.route.param("tab") === "settings" ? "settings" : "share";
}

function requestedGroup(): SettingsGroup {
  const group = m.route.param("group");
  return group === "account" || group === "backup" ? group : "general";
}

/** Keep ?tab=/?group= pointing at what is on screen (replace, no history entry). */
function rememberInUrl(param: string, value: string): void {
  const current = m.route.get();
  const [path, query = ""] = current.split("?");
  const params = new URLSearchParams(query);
  if (params.get(param) === value) return;
  params.set(param, value);
  m.route.set(`${path}?${params.toString()}`, undefined, { replace: true });
}

export const WorkspaceOptionsPage: m.ClosureComponent = () => {
  let model: WorkspaceOptionsModel | null = null;
  let tab: OptionsTab = "share";
  let group: SettingsGroup = "general";

  return {
    oninit() {
      const agentId = m.route.param("agentId");
      tab = requestedTab();
      group = requestedGroup();
      model = new WorkspaceOptionsModel(agentId);
      void model.load().then(() => {
        const target = m.route.param("target");
        if (target && model?.share) model.share.selectTarget(target);
      });
    },
    onremove() {
      model?.dispose();
    },
    view() {
      if (model === null) return null;
      return m(
        PageContainer,
        { extra: "flex flex-col min-h-0" },
        m(OptionsPanel, {
          model,
          tab,
          group,
          onSelectTab: (nextTab: OptionsTab) => {
            tab = nextTab;
            rememberInUrl("tab", nextTab);
          },
          onSelectGroup: (nextGroup: SettingsGroup) => {
            group = nextGroup;
            rememberInUrl("group", nextGroup);
          },
        }),
      );
    },
  };
};
