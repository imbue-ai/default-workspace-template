/**
 * Per-agent model + fast-mode state for the composer model picker.
 *
 * The backend exposes the agent's Claude Code selection (read from its
 * settings.json) and applies changes by sending `/model` / `/fast` slash
 * commands to the running session (see server.py). This module fetches that
 * state, caches it per agent, and posts changes -- optimistically reflecting the
 * new selection right away, then reconciling against the agent's real settings a
 * moment later (Claude Code persists the command asynchronously).
 */

import m from "mithril";
import { apiUrl } from "../base-path";

export interface ModelOption {
  id: string;
  label: string;
  supports_fast_mode: boolean;
}

export interface ModelSettings {
  model: string;
  fast_mode: boolean;
  fast_mode_supported: boolean;
  options: ModelOption[];
}

const settingsByAgent = new Map<string, ModelSettings>();
const inFlightFetch = new Set<string>();

// Claude Code writes the `/model` / `/fast` change to settings.json a beat after
// it accepts the command, so re-read shortly after posting to pick up the real
// persisted state (model label, fast-mode support, fast-mode value).
const RECONCILE_DELAY_MS = 600;

/** Bare alias of a model string, matching the backend's `base_alias`
 *  (`opus[1m]` -> `opus`), so a stored `opus` or `opus[1m]` both map to the
 *  Opus catalog option. */
export function baseAlias(model: string): string {
  return model.split("[")[0].trim().toLowerCase();
}

export function getModelSettings(agentId: string): ModelSettings | null {
  return settingsByAgent.get(agentId) ?? null;
}

/** The catalog option currently selected for the agent, matched by bare alias. */
export function getSelectedOption(agentId: string): ModelOption | null {
  const settings = settingsByAgent.get(agentId);
  if (!settings) {
    return null;
  }
  const currentAlias = baseAlias(settings.model);
  return settings.options.find((option) => baseAlias(option.id) === currentAlias) ?? null;
}

export async function fetchModelSettings(agentId: string): Promise<void> {
  if (inFlightFetch.has(agentId)) {
    return;
  }
  inFlightFetch.add(agentId);
  try {
    const settings = await m.request<ModelSettings>({
      method: "GET",
      url: apiUrl("/api/agents/:agentId/model-settings"),
      params: { agentId },
    });
    settingsByAgent.set(agentId, settings);
    m.redraw();
  } catch (error) {
    console.warn(`Failed to load model settings for agent ${agentId}`, error);
  } finally {
    inFlightFetch.delete(agentId);
  }
}

function scheduleReconcile(agentId: string): void {
  setTimeout(() => {
    fetchModelSettings(agentId);
  }, RECONCILE_DELAY_MS);
}

export async function setModel(agentId: string, modelId: string): Promise<void> {
  const current = settingsByAgent.get(agentId);
  if (current) {
    // Optimistic: reflect the pick immediately so the picker feels responsive.
    // fast_mode_supported follows the newly chosen model, and fast mode cannot
    // be on for a model that does not support it.
    const chosen = current.options.find((option) => option.id === modelId);
    const supportsFast = chosen?.supports_fast_mode ?? false;
    settingsByAgent.set(agentId, {
      ...current,
      model: modelId,
      fast_mode_supported: supportsFast,
      fast_mode: supportsFast ? current.fast_mode : false,
    });
    m.redraw();
  }
  await m.request({
    method: "POST",
    url: apiUrl("/api/agents/:agentId/model"),
    params: { agentId },
    body: { model: modelId },
  });
  scheduleReconcile(agentId);
}

export async function setFastMode(agentId: string, enabled: boolean): Promise<void> {
  const current = settingsByAgent.get(agentId);
  if (current) {
    settingsByAgent.set(agentId, { ...current, fast_mode: enabled });
    m.redraw();
  }
  await m.request({
    method: "POST",
    url: apiUrl("/api/agents/:agentId/fast"),
    params: { agentId },
    body: { enabled },
  });
  scheduleReconcile(agentId);
}
