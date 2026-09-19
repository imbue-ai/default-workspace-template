/**
 * The shell's own update notice: the same band a recently updated app's tabs carry, shown as a
 * top banner beside the staleness banner when the apply changed the shell itself.
 */

import m from "mithril";
import { SHELL_APP_NAME } from "../models/UpdateNotice";
import { UpdateNoticeBand } from "./UpdateNoticeBand";

export const UPDATE_NOTICE_BANNER_MARKER = "update-notice-banner";

export const UpdateNoticeBanner: m.Component = {
  view() {
    return m(UpdateNoticeBand, { appName: SHELL_APP_NAME, marker: UPDATE_NOTICE_BANNER_MARKER });
  },
};
