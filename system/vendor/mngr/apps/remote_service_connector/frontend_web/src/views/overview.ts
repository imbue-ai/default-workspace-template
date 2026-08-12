// The overview: one tile per synced workspace record, with live health from
// the share gateway's /_health probe (falling back to connector share/lease
// state to disambiguate), plus resumable pending creates.

import m from "mithril";
import {
  type LeasedHost,
  type WireRecord,
  listHosts,
  listRecords,
  releaseHost,
  shareStatus,
} from "../api";
import { probeWorkspaceHealth, workspaceEntryHost } from "../exec";
import {
  type PendingCreate,
  discardPendingCreate,
  loadPendingCreates,
  pushRecordWithCas,
} from "../records";

export type TileHealth =
  | "checking"
  | "healthy"
  | "degraded"
  | "unreachable"
  | "not_shared"
  | "destroyed";

export interface Tile {
  record: WireRecord;
  health: TileHealth;
  workspaceDomain: string | null;
}

// Exported for tests: the tile-state decision table is where "share exists
// but the tunnel is dead" must stay distinguishable from "not shared".
export async function resolveTileHealth(tile: Tile): Promise<void> {
  if (tile.record.state === "destroyed") {
    tile.health = "destroyed";
    return;
  }
  const status = await shareStatus(tile.record.host_id).catch(() => null);
  if (status === null || status.state !== "active") {
    tile.health = "not_shared";
    return;
  }
  tile.workspaceDomain = status.workspace_domain;
  const health = await probeWorkspaceHealth(
    workspaceEntryHost(status.workspace_domain, status.entry_label),
  );
  if (!health.reachable) {
    tile.health = "unreachable";
  } else if (health.detail !== null && health.detail.backend !== "ok") {
    tile.health = "degraded";
  } else {
    tile.health = "healthy";
  }
}

const HEALTH_BADGES: Record<TileHealth, [string, string]> = {
  checking: ["...", "bg-slate-200 text-slate-600"],
  healthy: ["healthy", "bg-emerald-100 text-emerald-700"],
  degraded: ["degraded", "bg-amber-100 text-amber-700"],
  unreachable: ["unreachable", "bg-red-100 text-red-700"],
  not_shared: ["desktop-only", "bg-slate-200 text-slate-600"],
  destroyed: ["destroyed", "bg-slate-200 text-slate-500"],
};

export function OverviewView(): m.Component {
  let tiles: Tile[] = [];
  let pendings: PendingCreate[] = [];
  // Pending creates whose workspace record is already destroyed: not
  // resumable (the workspace is gone), only discardable.
  let stalePendingHostIds = new Set<string>();
  let loading = true;
  let error = "";

  async function load(): Promise<void> {
    loading = true;
    error = "";
    try {
      const [records, hosts, pendingCreates] = await Promise.all([
        listRecords(),
        listHosts().catch(() => [] as LeasedHost[]),
        loadPendingCreates(),
      ]);
      const recordHostIds = new Set(records.map((record) => record.host_id));
      const destroyedHostIds = new Set(
        records
          .filter((record) => record.state === "destroyed")
          .map((record) => record.host_id),
      );
      pendings = pendingCreates;
      stalePendingHostIds = new Set(
        pendings
          .filter((pending) => destroyedHostIds.has(pending.host_id))
          .map((pending) => pending.host_id),
      );
      tiles = records.map((record) => ({
        record,
        health: "checking" as TileHealth,
        workspaceDomain: null,
      }));
      loading = false;
      m.redraw();
      await Promise.all(
        tiles.map((tile) => resolveTileHealth(tile).then(() => m.redraw())),
      );
      // Surface claimed-but-recordless leases as resumable orphans.
      for (const host of hosts) {
        if (
          !recordHostIds.has(host.host_id) &&
          !pendings.some((p) => p.host_id === host.host_id)
        ) {
          error = `Lease ${host.host_name} (${host.host_id}) has no workspace record; resume or destroy it from the desktop for now.`;
        }
      }
    } catch (loadError) {
      error = `Could not load workspaces: ${String(loadError)}`;
      loading = false;
    }
    m.redraw();
  }

  async function destroyWorkspace(tile: Tile): Promise<void> {
    const name = tile.record.display_name || tile.record.host_id;
    if (
      !window.confirm(
        `Destroy "${name}"? The workspace and its data will be deleted.`,
      )
    ) {
      return;
    }
    try {
      // Look the lease up at destroy time (no swallowed errors): a stale or
      // failed load-time snapshot must not let the release be skipped
      // silently, or the workspace would keep running (and counting against
      // the quota) behind a tombstoned record.
      const hosts = await listHosts();
      const lease =
        hosts.find((host) => host.host_id === tile.record.host_id) ?? null;
      if (lease !== null) {
        await releaseHost(lease.host_db_id);
      }
      await pushRecordWithCas(tile.record.host_id, (stored) => ({
        ...(stored ?? tile.record),
        state: "destroyed",
        revision: 0,
      }));
      await load();
    } catch (destroyError) {
      error = `Destroy failed: ${String(destroyError)}`;
      m.redraw();
    }
  }

  return {
    oninit() {
      void load();
    },
    view() {
      return m(
        "div",
        { class: "p-6 space-y-4" },
        m("h1", { class: "text-xl font-semibold" }, "Workspaces"),
        error ? m("p", { class: "text-sm text-red-600" }, error) : null,
        loading
          ? m("p", { class: "text-slate-500" }, "Loading workspaces...")
          : null,
        pendings.length > 0
          ? m(
              "div",
              { class: "space-y-2" },
              pendings.map((pending) => {
                const isStale = stalePendingHostIds.has(pending.host_id);
                return m(
                  "div",
                  {
                    class:
                      "rounded border border-amber-300 bg-amber-50 dark:bg-amber-950 p-4 flex items-center gap-4",
                  },
                  m(
                    "div",
                    { class: "grow" },
                    m("p", { class: "font-medium" }, pending.display_name),
                    m(
                      "p",
                      { class: "text-sm text-amber-700" },
                      isStale
                        ? "The workspace for this interrupted setup was destroyed; discard it."
                        : "Setup was interrupted; resume to finish.",
                    ),
                  ),
                  isStale
                    ? null
                    : m(
                        "button",
                        {
                          class:
                            "rounded bg-amber-600 text-white px-3 py-1 text-sm",
                          onclick() {
                            m.route.set(`/workspace/${pending.host_id}`);
                          },
                        },
                        "Resume",
                      ),
                  m(
                    "button",
                    {
                      class:
                        "rounded border border-amber-600 text-amber-700 px-3 py-1 text-sm",
                      async onclick() {
                        await discardPendingCreate(pending.host_id);
                        await load();
                      },
                    },
                    "Discard",
                  ),
                );
              }),
            )
          : null,
        !loading && tiles.length === 0 && pendings.length === 0
          ? m(
              "p",
              { class: "text-slate-500" },
              "No workspaces yet. ",
              m(
                m.route.Link,
                { href: "/create", class: "underline" },
                "Create one.",
              ),
            )
          : null,
        m(
          "div",
          { class: "grid gap-4 sm:grid-cols-2 lg:grid-cols-3" },
          tiles.map((tile) => {
            const [label, badgeClass] = HEALTH_BADGES[tile.health];
            const isOpenable =
              tile.record.state !== "destroyed" &&
              (tile.health === "healthy" || tile.health === "degraded");
            return m(
              "div",
              {
                class:
                  "rounded-lg border border-slate-200 dark:border-slate-800 p-4 space-y-3",
              },
              m(
                "div",
                { class: "flex items-center gap-2" },
                tile.record.color
                  ? m("span", {
                      class: "inline-block h-3 w-3 rounded-full",
                      style: `background:${tile.record.color}`,
                    })
                  : null,
                m(
                  "span",
                  { class: "font-medium truncate" },
                  tile.record.display_name || tile.record.host_id,
                ),
                m("div", { class: "grow" }),
                m(
                  "span",
                  { class: `rounded px-2 py-0.5 text-xs ${badgeClass}` },
                  label,
                ),
              ),
              m(
                "p",
                { class: "text-xs text-slate-500 truncate" },
                `${tile.record.provider_kind || "unknown"} - ${tile.record.host_id}`,
              ),
              m(
                "div",
                { class: "flex gap-2" },
                isOpenable
                  ? m(
                      "button",
                      {
                        class:
                          "rounded bg-slate-900 dark:bg-slate-100 text-white dark:text-slate-900 px-3 py-1 text-sm",
                        onclick() {
                          m.route.set(`/workspace/${tile.record.host_id}`);
                        },
                      },
                      "Open",
                    )
                  : null,
                tile.record.provider_kind === "imbue_cloud" &&
                  tile.record.state !== "destroyed"
                  ? m(
                      "button",
                      {
                        class:
                          "rounded border border-red-300 text-red-600 px-3 py-1 text-sm",
                        onclick: () => void destroyWorkspace(tile),
                      },
                      "Destroy",
                    )
                  : null,
              ),
            );
          }),
        ),
      );
    },
  };
}
