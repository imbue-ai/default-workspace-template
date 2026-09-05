/**
 * The instance verbs and the app lifecycle, through the shell's relay (contracts.md section 6).
 *
 * A browser never reaches an app's instances API: every create, delete, rename and location
 * report goes to the shell, which forwards it to the app and refetches the app's list. The
 * shell's answer is the app's, status and body alike, so a refusal reads here exactly as the
 * app spelled it.
 */

import { apiUrl } from "../base-path";
import { errorDetailFromResponse, postJson } from "./http";
import type { InstanceRecord } from "./Inventory";

function instancesUrl(appName: string, suffix: string = ""): string {
  return apiUrl(`/api/apps/${encodeURIComponent(appName)}/instances${suffix}`);
}

/** Run one of an app's actions, answering the instance it created. Throws with the app's detail. */
export async function createInstance(
  appName: string,
  actionId: string,
  params: Readonly<Record<string, string>> = {},
): Promise<InstanceRecord> {
  const data = await postJson<{ instance: InstanceRecord }>(instancesUrl(appName), { action: actionId, params });
  return data.instance;
}

/** Delete one instance; idempotent for an unknown key. Throws with the app's detail. */
export async function deleteInstance(appName: string, key: string): Promise<void> {
  await postJson<void>(instancesUrl(appName, `/${encodeURIComponent(key)}/delete`), {});
}

/** Retitle one instance. Throws with the app's detail (a conflict, an app that does not rename). */
export async function renameInstance(appName: string, key: string, title: string): Promise<InstanceRecord> {
  const data = await postJson<{ instance: InstanceRecord }>(
    instancesUrl(appName, `/${encodeURIComponent(key)}/rename`),
    { title },
  );
  return data.instance;
}

/** Tell an app where one of its instances is now looking (a path under its origin). Best-effort. */
export async function reportInstanceLocation(appName: string, key: string, path: string): Promise<void> {
  const response = await fetch(instancesUrl(appName, `/${encodeURIComponent(key)}/location`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!response.ok) {
    console.warn(`[si] ${appName} did not take the location of ${key}: ${await errorDetailFromResponse(response)}`);
  }
}

/** Stop or start an app's supervised program. The ``apps_updated`` push that follows is the
 *  authority on the result; this only reports a refusal. */
export async function setAppLifecycle(appName: string, action: "stop" | "start"): Promise<void> {
  await postJson<void>(apiUrl(`/api/apps/${encodeURIComponent(appName)}/${action}`), {});
}
