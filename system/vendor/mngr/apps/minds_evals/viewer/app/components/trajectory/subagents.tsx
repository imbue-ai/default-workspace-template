import { ChevronDown, ChevronRight, CornerDownRight, Redo2 } from "lucide-react";

import { asRecord } from "~/components/trajectory/json";
import { Toggle } from "~/components/trajectory/toggle";
import { cn } from "~/lib/utils";
import type { Step, Trajectory } from "~/lib/types";

/**
 * Delegated agents, whose trajectories ATIF embeds in the parent's `subagent_trajectories` and
 * points at from the observation of the step that spawned them.
 *
 * A minds eval produces two kinds, both stamped `subagent_kind: "mngr"`: background workers the
 * agent launches through the launch-task skill, which the harness captures on the host and grafts
 * on, and the agent's own `Task` delegations, which mngr runs as proxy sibling agents and embeds
 * when it builds the document. The parent's step shows only the tool call that started one, so
 * without this the whole of a delegated agent's work is absent from the trajectory it belongs to.
 *
 * A delegated agent may delegate in turn, so both of the ways to read one below recurse.
 */

/** How a delegated agent's steps are folded into the trajectory that spawned it. */
export type SubagentMode = "nested" | "flat";

const SUBAGENT_MODES: readonly SubagentMode[] = ["nested", "flat"];

const SUBAGENT_MODE_LABELS: Record<SubagentMode, string> = {
  nested: "Nested",
  flat: "Flat",
};

const SUBAGENT_MODE_HINTS: Record<SubagentMode, string> = {
  nested: "Collapsed under the step that spawned it",
  flat: "Every agent's steps on one timeline, in the order they happened",
};

// What an embedded agent's `extra` block is read for. Named rather than spelled inline so
// `viewer_contract_test.py` can pin them against the Python that writes them: no schema owns these
// keys, and nothing in the toolchain connects a TypeScript literal to the dict on the other side.
const SUBAGENT_KIND_KEY = "subagent_kind";
const WORKER_KEY = "worker";
const WORKER_NAME_KEY = "name";
const WORKER_STATE_KEY = "state";
// The one worker state that changes how the transcript beneath it reads.
const RUNNING_STATE = "running";

export interface Subagent {
  /**
   * Unique among its own parent's `subagent_trajectories`, which is as far as ATIF goes -- see
   * `runKey`, which keys on the whole lineage chain rather than on this. The open-disclosure set in
   * nested mode is the one place a bare id still stands for an agent, so two copies of one document
   * embedded under different parents open and close together.
   */
  trajectoryId: string;
  /**
   * The worker's name where the launcher recorded one, which is only the workers the harness
   * grafts on. Otherwise ATIF's `agent.name`, which names the harness and not the instance: mngr
   * writes the agent type there for the siblings it embeds, so every `Task` delegation in one
   * document reads `claude`, and the step it hangs from is what tells them apart.
   */
  label: string;
  kind: string;
  /**
   * What the worker was doing when the trial collected it: `running` means the transcript below
   * stops mid-flight rather than at the end of the work, which is otherwise indistinguishable from
   * an agent that simply finished quietly.
   */
  state: string;
  trajectory: Trajectory;
}

/** Whether a transcript ends because the work ended, or because collection caught it mid-flight. */
export function isStillRunning(subagent: Subagent): boolean {
  return subagent.state === RUNNING_STATE;
}

/** What the launcher recorded about the worker it started, or null for an agent no launcher named. */
function workerRecord(trajectory: Trajectory): Record<string, unknown> | null {
  return asRecord(asRecord(trajectory.extra)?.[WORKER_KEY]);
}

function subagentLabel(trajectory: Trajectory): string {
  const name = workerRecord(trajectory)?.[WORKER_NAME_KEY];
  return typeof name === "string" && name !== ""
    ? name
    : trajectory.agent.name;
}

function subagentState(trajectory: Trajectory): string {
  const state = workerRecord(trajectory)?.[WORKER_STATE_KEY];
  return typeof state === "string" ? state : "";
}

function subagentKind(trajectory: Trajectory): string {
  const kind = asRecord(trajectory.extra)?.[SUBAGENT_KIND_KEY];
  return typeof kind === "string" ? kind : "subagent";
}

/**
 * `trajectoryId` narrowed to an id that can key a delegated agent, or null for one that cannot.
 *
 * The empty string is as much of a gap as a missing key, since a producer building the id from a
 * field it does not have can write one, and the viewer parses its JSON without validating it.
 */
function resolvableId(
  trajectoryId: string | null | undefined
): string | null {
  return typeof trajectoryId === "string" && trajectoryId !== ""
    ? trajectoryId
    : null;
}

/** Every embedded subagent, keyed by the id its references resolve against. */
export function indexSubagents(
  trajectory: Trajectory | null | undefined
): Map<string, Subagent> {
  const index = new Map<string, Subagent>();
  for (const sub of trajectory?.subagent_trajectories ?? []) {
    const trajectoryId = resolvableId(sub.trajectory_id);
    if (trajectoryId === null) continue;
    index.set(trajectoryId, {
      trajectoryId,
      label: subagentLabel(sub),
      kind: subagentKind(sub),
      state: subagentState(sub),
      trajectory: sub,
    });
  }
  return index;
}

/** The delegated agents a step spawned, in the order its observation names them. */
function subagentsForStep(
  step: Step,
  index: Map<string, Subagent>
): Subagent[] {
  const found: Subagent[] = [];
  for (const result of step.observation?.results ?? []) {
    for (const ref of result.subagent_trajectory_ref ?? []) {
      const trajectoryId = resolvableId(ref.trajectory_id);
      const resolved =
        trajectoryId === null ? undefined : index.get(trajectoryId);
      // An unresolvable reference is one whose trajectory was not embedded -- an external file, or
      // a worker that never reported. Nothing to show, so it is skipped rather than stubbed.
      if (resolved !== undefined && !found.includes(resolved)) {
        found.push(resolved);
      }
    }
  }
  return found;
}

/** Which delegated agent belongs where in a trajectory's step list. */
export interface SpawnedSubagents {
  /** What each step spawned, one entry per step, in step order. */
  perStep: Subagent[][];
  /** Embedded agents that no step names. A launch whose step is missing from the document still
   *  embeds its worker, so these carry real work and are shown rather than dropped. */
  unattributed: Subagent[];
}

/**
 * Where each embedded agent belongs among `steps`.
 *
 * A delegated agent belongs to the step that started it, so a later step naming the same one -- an
 * await following a launch, say -- gets nothing for it, and its work is drawn once.
 */
export function subagentsByStep(
  steps: Step[],
  index: Map<string, Subagent>
): SpawnedSubagents {
  const taken = new Set<string>();
  const perStep = steps.map((step) => {
    const spawned: Subagent[] = [];
    for (const subagent of subagentsForStep(step, index)) {
      if (taken.has(subagent.trajectoryId)) continue;
      taken.add(subagent.trajectoryId);
      spawned.push(subagent);
    }
    return spawned;
  });
  const unattributed = [...index.values()].filter(
    (subagent) => !taken.has(subagent.trajectoryId)
  );
  return { perStep, unattributed };
}

function stepsLabel(count: number): string {
  return `${count} ${count === 1 ? "step" : "steps"}`;
}

/** The classes one level of delegation is drawn in: its rail, the wash the rail widens into, and
 *  the label beside them. */
interface Shade {
  rail: string;
  wash: string;
  label: string;
}

// Written out in full because Tailwind reads class names out of the source and never sees one that
// was assembled at runtime. A rail and the label beside it run on separate scales, since a 2px line
// reads at a lightness a word does not; the wash is the rail's own colour at a fraction of its
// weight, so a deeper level's band darkens exactly as far as its line does. That fraction is smaller
// on a dark ground, where a wash lightens away from the card instead of darkening into it and the
// same strength would out-shout every step surface on the page. Adding a level means adding a shade
// here and a custom property per scale in app.css.
const SHADES: readonly [Shade, Shade, Shade, Shade] = [
  {
    rail: "border-subagent-rail-1",
    wash: "border-subagent-rail-1/35 dark:border-subagent-rail-1/20",
    label: "text-subagent-label-1",
  },
  {
    rail: "border-subagent-rail-2",
    wash: "border-subagent-rail-2/35 dark:border-subagent-rail-2/20",
    label: "text-subagent-label-2",
  },
  {
    rail: "border-subagent-rail-3",
    wash: "border-subagent-rail-3/35 dark:border-subagent-rail-3/20",
    label: "text-subagent-label-3",
  },
  {
    rail: "border-subagent-rail-4",
    wash: "border-subagent-rail-4/35 dark:border-subagent-rail-4/20",
    label: "text-subagent-label-4",
  },
];

/** How a level that deep is drawn. Delegation deeper than the scale runs reuses its last shade. */
function shade(depth: number): Shade {
  return SHADES[Math.min(Math.max(depth, 1), SHADES.length) - 1];
}

/**
 * The band a delegated agent's steps sit behind, coloured for how deep the delegation runs: a crisp
 * edge widening into a wash of the same colour, so one level reads as one object rather than as a
 * line with a tint beside it.
 *
 * The padding is what keeps the band unbroken. A step block bleeds `-mx-6` past its container so its
 * background can span the card, which lands it 1.5rem left of the content box; any band past that
 * point is painted over wherever a step has a background of its own. So the padding must clear
 * 1.5rem, and what it clears it by is the gap between the band and the step's own coloured left
 * border -- with no gap the two butt together and read as a single two-tone line.
 */
function SubagentRail({
  depth,
  children,
}: {
  depth: number;
  children: React.ReactNode;
}) {
  return (
    <div className={cn("border-l-2", shade(depth).rail)}>
      <div className={cn("border-l-8 pl-7", shade(depth).wash)}>{children}</div>
    </div>
  );
}

/**
 * `children` behind one band per level of delegation, outermost first.
 *
 * A row three levels down sits inside the bands of the agents it is nested in, rather than beside a
 * single band of its own: on a timeline that changes hands constantly, the bands to the left of a
 * step are what say whose work it is nested in without the reader having to read a name.
 *
 * An empty lineage -- the viewed trajectory's own rows, which nothing delegated -- adds no band, so
 * a caller can wrap every row alike instead of branching on depth.
 */
export function SubagentRails({
  lineage,
  children,
}: {
  lineage: readonly Subagent[];
  children: React.ReactNode;
}) {
  return lineage.reduceRight<React.ReactNode>(
    (inner, agent, index) => (
      <SubagentRail key={agent.trajectoryId} depth={index + 1}>
        {inner}
      </SubagentRail>
    ),
    children
  );
}

// Lines a delegated agent's own header up with the step headers beneath it: the half-unit makes
// up the transparent left border every step block carries, and the marker hangs out in the rail's
// padding instead of pushing the label right.
const SUBAGENT_HEADER_ALIGNMENT = "relative pl-0.5";
const SUBAGENT_HEADER_MARKER = "absolute -left-5 size-3.5";

export function SubagentModeToggle({
  mode,
  onModeChange,
}: {
  mode: SubagentMode;
  onModeChange: (next: SubagentMode) => void;
}) {
  return (
    <div className="flex items-center gap-1">
      <span className="mr-1 text-xs uppercase text-muted-foreground">
        Subagents
      </span>
      {SUBAGENT_MODES.map((name) => (
        <Toggle
          key={name}
          label={SUBAGENT_MODE_LABELS[name]}
          pressed={name === mode}
          title={SUBAGENT_MODE_HINTS[name]}
          onPressedChange={() => onModeChange(name)}
        />
      ))}
    </div>
  );
}

/** What a transcript that stops mid-flight says about itself, in both ways of reading. */
const STILL_RUNNING_NOTE = "still running when collected";

const METADATA_SEPARATOR = " · ";

/**
 * What trails an agent's name: what it is, how big it is, and whether collection caught it
 * mid-flight. Absent parts drop out, and the whole strip with them, so a run with nothing to report
 * does not grow a stray separator.
 */
function SubagentMetadata({ parts }: { parts: readonly (string | null)[] }) {
  // Empty as well as null: `subagentKind` falls back only for a missing key and passes an empty
  // `subagent_kind` on as it found it, which would put a separator either side of nothing.
  const present = parts.filter((part) => part !== null && part !== "");
  if (present.length === 0) return null;
  return (
    <span className="text-muted-foreground">
      {METADATA_SEPARATOR}
      {present.join(METADATA_SEPARATOR)}
    </span>
  );
}

/**
 * Who holds the timeline, at the point where it changes hands.
 *
 * An agent yields and takes the floor back repeatedly, so most of these head a resumption rather
 * than a beginning. Only the first carries the down-and-right arrow, the agent's kind and the size
 * of its whole trajectory; the rest carry a resume marker and the length of the stretch they head,
 * because the same arrow on every one of them reads as the agent starting afresh each time.
 *
 * The name of the agent that holds the timeline is what the reader is scanning for, so it is the
 * only part in full strength: the agents it is nested under lead up to it, and what is known about
 * the run trails after it, both in the muted tone the step headers beneath use for their metadata.
 */
export function SubagentRunHeader({
  lineage,
  kind,
  rootLabel,
  runLength,
  totalLength,
  isRunning,
  depth,
  isFirstRun,
}: {
  /** Every agent from the viewed trajectory down to this row's, outermost first. Empty at depth 0. */
  lineage: readonly Subagent[];
  kind: string | null;
  /** What to call the run when `lineage` is empty, which is the viewed trajectory's own. */
  rootLabel: string;
  /** Steps in the stretch this heads, which is what a resumption reports. */
  runLength: number | null;
  /** Steps in the agent's whole trajectory, which is what its opening reports. */
  totalLength: number | null;
  isRunning: boolean;
  depth: number;
  isFirstRun: boolean;
}) {
  const name = lineage[lineage.length - 1]?.label ?? rootLabel;
  const size = isFirstRun ? totalLength : runLength;
  const Marker = isFirstRun ? CornerDownRight : Redo2;
  return (
    <div
      data-subagent-run={name}
      data-run-start={isFirstRun ? "first" : "resumed"}
      className={cn(
        // Set like the step metadata beneath rather than like the chrome above: an agent's name is
        // content, and these sit in the reading column where uppercase would outweigh every step.
        "flex items-center py-1 text-xs",
        // The viewed trajectory's own runs have no band to align to, or to hang a marker in.
        depth === 0
          ? "text-muted-foreground"
          : cn(SUBAGENT_HEADER_ALIGNMENT, shade(depth).label)
      )}
    >
      <Marker
        className={cn(
          "size-3.5 shrink-0",
          depth === 0 ? "mr-2" : SUBAGENT_HEADER_MARKER
        )}
        aria-hidden="true"
      />
      <span className="min-w-0 truncate">
        {lineage.slice(0, -1).map((ancestor) => (
          <span key={ancestor.trajectoryId} className="text-muted-foreground">
            {ancestor.label} →{" "}
          </span>
        ))}
        {name}
        <SubagentMetadata
          parts={[
            isFirstRun ? kind : null,
            size === null
              ? null
              : isFirstRun
                ? stepsLabel(size)
                : `${stepsLabel(size)} more`,
            isFirstRun && isRunning ? STILL_RUNNING_NOTE : null,
          ]}
        />
      </span>
    </div>
  );
}

/** The collapsible header a delegated agent's steps hang from, in nested mode. */
export function SubagentDisclosure({
  subagent,
  depth,
  open,
  onToggle,
  children,
}: {
  subagent: Subagent;
  depth: number;
  open: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <SubagentRail depth={depth}>
      <div className="mt-2" data-subagent={subagent.label}>
        <button
          type="button"
          aria-expanded={open}
          onClick={onToggle}
          className={cn(
            "flex w-full cursor-pointer items-center py-1 text-left text-xs",
            SUBAGENT_HEADER_ALIGNMENT,
            shade(depth).label
          )}
        >
          {open ? (
            <ChevronDown className={SUBAGENT_HEADER_MARKER} aria-hidden="true" />
          ) : (
            <ChevronRight className={SUBAGENT_HEADER_MARKER} aria-hidden="true" />
          )}
          <span className="min-w-0 truncate">
            {subagent.label}
            <SubagentMetadata
              parts={[
                subagent.kind,
                stepsLabel(subagent.trajectory.steps.length),
                isStillRunning(subagent) ? STILL_RUNNING_NOTE : null,
              ]}
            />
          </span>
        </button>
        {open && children}
      </div>
    </SubagentRail>
  );
}

/**
 * Which agent a timeline row belongs to, as the chain of ids from the viewed trajectory down to it.
 *
 * Not the agent's own `trajectoryId`: ATIF requires that to be unique only among one parent's
 * `subagent_trajectories`, so one document embedded under two parents is two agents sharing an id,
 * and keying on it alone would run their rows together under the first one's name and bands. The
 * ids are stringified as a list so that an id containing the separator cannot forge another chain.
 *
 * A depth-0 row's lineage is empty, and the empty list is a key no chain of any length can produce,
 * so the viewed trajectory's own rows have a key of their own whatever ids are embedded beneath it.
 */
export function runKey(row: TimelineRow): string {
  return JSON.stringify(row.lineage.map((agent) => agent.trajectoryId));
}

/** One step on the flat timeline, with the agent it came from. */
export interface TimelineRow {
  step: Step;
  /** The step's position in its own trajectory, which is where its step ref and highlight key off. */
  index: number;
  /** 0 for the trajectory being viewed, 1 for what it delegated, and so on. */
  depth: number;
  /** The delegated agent the step belongs to, or null for the trajectory being viewed. */
  subagent: Subagent | null;
  /** Every agent between the viewed trajectory and this row's, outermost first. Empty at depth 0. */
  lineage: readonly Subagent[];
  /** Set on the first row after the timeline changes hands, which is where it needs a header. */
  startsRun: boolean;
  /** How many rows this row's agent holds the timeline for before handing it back. An agent that
   *  yields and resumes has several runs, so this is not its step count. */
  runLength: number;
}

interface TimedRow extends Omit<TimelineRow, "startsRun" | "runLength"> {
  /** Milliseconds since the epoch, or -Infinity for a step no timestamp could be found for. */
  at: number;
}

function stepTime(step: Step): number | null {
  if (step.timestamp === null) return null;
  const parsed = Date.parse(step.timestamp);
  return Number.isNaN(parsed) ? null : parsed;
}

/** Where a trajectory's steps start out, before any of its own timestamps are read. */
interface TimelineStart {
  /** The time a step with no timestamp of its own takes. */
  inheritedAt: number;
  /** The earliest time any of its steps may sort at. */
  earliestAt: number;
}

/**
 * Every step of `trajectory` and of everything it delegated to, appended in the order they were
 * spawned so that a stable sort by time leaves a parent step ahead of the work it started.
 *
 * A step with no timestamp of its own inherits the last time seen in its own trajectory, falling
 * back to the time the trajectory entered the timeline, so a delegated agent that timestamps
 * nothing lands where it was spawned -- the best guess available.
 *
 * Time only decides which agent holds the floor. Within one trajectory the document's own order is
 * authoritative, so the time a step sorts on never moves backwards.
 */
function collectTimedRows(
  trajectory: Trajectory,
  depth: number,
  subagent: Subagent | null,
  lineage: readonly Subagent[],
  start: TimelineStart,
  into: TimedRow[]
): void {
  const spawned = subagentsByStep(
    trajectory.steps,
    indexSubagents(trajectory)
  );
  let previous = start.inheritedAt;
  let earliest = start.earliestAt;
  trajectory.steps.forEach((step, stepIndex) => {
    // Never before the step before it. Two clocks meet in one document and neither is trustworthy
    // to the second: a delegated agent runs in a box of its own, and the harness stamps its
    // step_boundary markers on the host while the steps around them are stamped in the box, so
    // drift either way would carry a marker off the step it heads.
    const at = Math.max(stepTime(step) ?? previous, earliest);
    previous = at;
    earliest = at;
    into.push({ step, index: stepIndex, depth, subagent, lineage, at });
    for (const child of spawned.perStep[stepIndex] ?? []) {
      // A step the document places may not sort above the tool call that launched it: a few seconds
      // of skew between the two boxes would otherwise open the worker's transcript above its launch.
      collectTimedRows(
        child.trajectory,
        depth + 1,
        child,
        [...lineage, child],
        { inheritedAt: at, earliestAt: at },
        into
      );
    }
  });
  for (const child of spawned.unattributed) {
    // No launch to sit after, so its own clock alone decides where it goes, and only one that
    // timestamps nothing at all falls back to the end of the list it was embedded in.
    collectTimedRows(
      child.trajectory,
      depth + 1,
      child,
      [...lineage, child],
      { inheritedAt: previous, earliestAt: Number.NEGATIVE_INFINITY },
      into
    );
  }
}

/** The merged timeline: every agent's steps in one list, in the order they happened. */
export function buildTimeline(trajectory: Trajectory): TimelineRow[] {
  const collected: TimedRow[] = [];
  const unanchored: TimelineStart = {
    inheritedAt: Number.NEGATIVE_INFINITY,
    earliestAt: Number.NEGATIVE_INFINITY,
  };
  collectTimedRows(trajectory, 0, null, [], unanchored, collected);
  // Compared rather than subtracted, so the -Infinity an untimestamped root step carries does not
  // come out of the comparator as NaN. The sort is stable, so rows sharing a time keep the spawn
  // order they were collected in, which puts a parent step ahead of the work it started.
  collected.sort((left, right) =>
    left.at === right.at ? 0 : left.at < right.at ? -1 : 1
  );

  const rows: TimelineRow[] = collected.map((row) => ({
    step: row.step,
    index: row.index,
    depth: row.depth,
    subagent: row.subagent,
    lineage: row.lineage,
    startsRun: false,
    runLength: 0,
  }));
  // An agent holds the timeline until another takes it, and can take it back later, so a run is a
  // stretch of adjacent rows rather than everything that agent contributed.
  let start = 0;
  for (let position = 1; position <= rows.length; position++) {
    if (position < rows.length && runKey(rows[position]) === runKey(rows[start]))
      continue;
    rows[start].startsRun = true;
    for (let inRun = start; inRun < position; inRun++) {
      rows[inRun].runLength = position - start;
    }
    start = position;
  }
  return rows;
}
