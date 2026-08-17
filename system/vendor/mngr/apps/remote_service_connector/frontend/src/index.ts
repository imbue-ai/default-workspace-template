// Entry point for the hosted accounts pages. One bundle serves /login,
// /signup, /manage, and the utility pages (the backend returns index.html
// for each); the path picks the page component.

import m from "mithril";
import "./style.css";
import { AuthPage } from "./AuthPage";
import { ManagePage } from "./ManagePage";
import {
  CheckInboxPage,
  ResetPasswordPage,
  VerifyEmailPage,
} from "./UtilityPages";

const PAGE_BY_PATH: Record<string, () => m.Component> = {
  "/manage": ManagePage,
  "/auth/reset-password": ResetPasswordPage,
  "/auth/verify-email": VerifyEmailPage,
  "/check-inbox": CheckInboxPage,
};

// Theme follows the OS: there is no app chrome here to own a theme toggle.
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
  const page = PAGE_BY_PATH[window.location.pathname] ?? AuthPage;
  m.mount(root, page);
}

main();
