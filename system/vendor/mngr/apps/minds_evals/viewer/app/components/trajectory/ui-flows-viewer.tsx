import { useQuery } from "@tanstack/react-query";
import { Compass } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { CodeBlock } from "~/components/ui/code-block";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "~/components/ui/empty";
import { API_BASE, encodePathSegments, fetchTrialFile } from "~/lib/api";
import { cn } from "~/lib/utils";

/**
 * The UI flows an eval runs against the finished workspace: a browser agent given a short script and
 * an expectation, driving the app the workspace built and screenshotting every step.
 *
 * The evidence is spread over two shapes. `verification/manifest.json` holds one entry per flow --
 * the verdict, and the prose the judge is shown -- and is the authoritative list, since a flow that
 * never ran still has an entry. `verification/flows/<name>/log.jsonl` holds what the agent actually
 * did, one record per step, with its screenshot beside it.
 */
const VERIFICATION_DIR = "agent/verification";
const FLOW_CHECK_CLASS = "ui_flows";
// Entry ids are `ui_flow_<n>_<name>`, and a step-scoped one carries the prefix its scope adds.
const FLOW_ENTRY_ID = /^(?:step_)?ui_flow_\d+_(.+)$/;

export interface UiFlow {
  entryId: string;
  name: string;
  /** "step" or "task" once a case declares both sets; absent on jobs recorded before scopes. */
  scope: string | null;
  status: string;
  reason: string;
  detail: string;
}

/**
 * One line of a flow's log. `kind` says how to read it -- `init` opens the flow with what it was
 * asked to do and the page it landed on, `action` is a step, `final` is the agent's closing reading.
 *
 * A kind this viewer does not know is kept and shown as its raw record rather than dropped, so a
 * producer can add one without the viewer having to learn it first. A log written before kinds
 * existed carries none, and its records are steps.
 */
export interface FlowStep {
  kind: string;
  index: number;
  timestamp: string | null;
  action: string;
  reasoning: string;
  saw: string;
  expected: string;
  observed: string;
  goal: string;
  expect: string;
  url: string;
  state: string;
  screenshot: string;
  error: string;
  /** The record as it arrived, for a kind with no rendering of its own. */
  raw: string;
}

const KNOWN_KINDS: ReadonlySet<string> = new Set(["init", "action", "final"]);

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asText(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function parseFlows(manifest: string): UiFlow[] {
  let document: unknown;
  try {
    document = JSON.parse(manifest);
  } catch {
    return [];
  }
  const entries = asRecord(document)?.entries;
  if (!Array.isArray(entries)) return [];

  const flows: UiFlow[] = [];
  for (const raw of entries) {
    const entry = asRecord(raw);
    if (entry === null || entry.check_class !== FLOW_CHECK_CLASS) continue;
    const entryId = asText(entry.entry_id);
    const named = FLOW_ENTRY_ID.exec(entryId);
    if (named === null) continue;
    flows.push({
      entryId,
      name: named[1],
      scope: typeof entry.scope === "string" ? entry.scope : null,
      status: asText(entry.status),
      reason: asText(entry.reason),
      detail: asText(entry.detail),
    });
  }
  return flows;
}

function parseFlowLog(log: string): FlowStep[] {
  const steps: FlowStep[] = [];
  for (const line of log.split("\n")) {
    if (line.trim() === "") continue;
    let record: unknown;
    try {
      record = JSON.parse(line);
    } catch {
      continue;
    }
    const entry = asRecord(record);
    if (entry === null) continue;
    steps.push({
      kind: asText(entry.kind) || "action",
      index: typeof entry.step_index === "number" ? entry.step_index : steps.length + 1,
      timestamp: typeof entry.timestamp === "string" ? entry.timestamp : null,
      action: asText(entry.action),
      reasoning: asText(entry.reasoning),
      saw: asText(entry.saw),
      expected: asText(entry.expected),
      observed: asText(entry.observed),
      goal: asText(entry.goal),
      expect: asText(entry.expect),
      url: asText(entry.url),
      state: asText(entry.state),
      screenshot: asText(entry.screenshot),
      error: asText(entry.error),
      raw: line,
    });
  }
  return steps;
}

/**
 * The flows recorded for a trial. Exported so the trial page can decide whether to offer the tab at
 * all; react-query dedupes the two callers onto one request.
 */
export function useUiFlows(
  jobName: string,
  trialName: string,
  step?: string | null,
  enabled = true
) {
  return useQuery({
    enabled,
    queryKey: ["ui-flows", jobName, trialName, step],
    queryFn: async () => {
      // A trial that collected no evidence has no manifest, which is a normal outcome, not an error.
      const manifest = await fetchTrialFile(
        jobName,
        trialName,
        `${VERIFICATION_DIR}/manifest.json`,
        step
      ).catch(() => null);
      return manifest === null ? [] : parseFlows(manifest);
    },
  });
}

function flowFileUrl(
  jobName: string,
  trialName: string,
  flowName: string,
  fileName: string,
  step?: string | null
): string {
  const path = encodePathSegments(
    `${VERIFICATION_DIR}/flows/${flowName}/${fileName}`
  );
  const query = step ? `?step=${encodeURIComponent(step)}` : "";
  return `${API_BASE}/api/jobs/${encodeURIComponent(jobName)}/trials/${encodeURIComponent(trialName)}/files/${path}${query}`;
}

function elapsedLabel(from: string | null, to: string | null): string | null {
  if (from === null || to === null) return null;
  const seconds = (Date.parse(to) - Date.parse(from)) / 1000;
  if (!Number.isFinite(seconds) || seconds < 0) return null;
  return seconds < 60
    ? `+${seconds.toFixed(1)}s`
    : `+${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

function Toggle({
  label,
  pressed,
  onPressedChange,
}: {
  label: string;
  pressed: boolean;
  onPressedChange: (next: boolean) => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      onClick={() => onPressedChange(!pressed)}
      className={cn(
        "cursor-pointer px-2 py-0.5 text-xs uppercase transition-colors",
        pressed
          ? "bg-muted text-foreground"
          : "text-muted-foreground hover:text-foreground"
      )}
    >
      {label}
    </button>
  );
}

function statusClass(status: string): string {
  return status === "passed" ? "text-foreground" : "text-destructive";
}

function FlowList({
  flows,
  selected,
  onSelect,
}: {
  flows: UiFlow[];
  selected: string;
  onSelect: (entryId: string) => void;
}) {
  // Scope only earns a heading once a case declares more than one set of expectations.
  const scopes = [...new Set(flows.map((flow) => flow.scope ?? ""))];
  return (
    <div className="flex flex-col">
      {scopes.map((scope) => (
        <div key={scope}>
          {scopes.length > 1 && (
            <div className="px-3 py-1 text-xs uppercase text-muted-foreground">
              {scope === "" ? "unscoped" : scope}
            </div>
          )}
          {flows
            .filter((flow) => (flow.scope ?? "") === scope)
            .map((flow) => (
              <button
                key={flow.entryId}
                type="button"
                data-ui-flow={flow.name}
                onClick={() => onSelect(flow.entryId)}
                className={cn(
                  "flex w-full cursor-pointer flex-col items-start gap-0.5 border-l-2 px-3 py-2 text-left transition-colors",
                  flow.entryId === selected
                    ? "border-foreground bg-muted"
                    : "border-transparent hover:bg-muted/50"
                )}
              >
                <span className="text-sm">{flow.name}</span>
                <span className={cn("text-xs", statusClass(flow.status))}>
                  {flow.status}
                  {flow.reason !== "" ? ` · ${flow.reason}` : ""}
                </span>
              </button>
            ))}
        </div>
      ))}
    </div>
  );
}

/** One labelled line of a step's account of itself. */
function Term({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="whitespace-nowrap text-xs uppercase text-muted-foreground">
        {label}
      </dt>
      <dd className="min-w-0 whitespace-pre-wrap text-muted-foreground">
        {children}
      </dd>
    </>
  );
}

/** The flow's opening: what it was asked to do, and the page it opened onto. */
function FlowOpening({
  step,
  screenshotUrl,
  showScreenshots,
  showState,
}: {
  step: FlowStep;
  screenshotUrl: string | null;
  showScreenshots: boolean;
  showState: boolean;
}) {
  return (
    <div className="border-t py-4 first:border-t-0" data-flow-opening="">
      <div className="text-xs uppercase text-muted-foreground">start</div>
      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
        {step.goal !== "" && <Term label="goal">{step.goal}</Term>}
        {step.expect !== "" && <Term label="expect">{step.expect}</Term>}
        {step.url !== "" && <Term label="opened">{step.url}</Term>}
      </dl>
      {showScreenshots && screenshotUrl !== null && (
        <img
          src={screenshotUrl}
          alt="The page the flow opened onto"
          loading="lazy"
          className="mt-3 max-h-[32rem] w-auto max-w-full border object-contain object-left"
        />
      )}
      {showState && step.state !== "" && (
        <div className="mt-3">
          <CodeBlock code={step.state} lang="text" wrap />
        </div>
      )}
    </div>
  );
}

/**
 * A record whose kind this viewer has no rendering for, shown as it arrived.
 *
 * Kept rather than skipped: a producer that starts writing a new kind should be visible here
 * immediately, and a reader who can see the record can tell what is missing.
 */
function UnknownRecord({ step }: { step: FlowStep }) {
  return (
    <details className="border-t py-4 first:border-t-0">
      <summary className="cursor-pointer text-sm text-muted-foreground">
        #{step.index} · unrecognised record ({step.kind})
      </summary>
      <div className="mt-3">
        <CodeBlock code={step.raw} lang="json" wrap />
      </div>
    </details>
  );
}

function FlowStepBlock({
  step,
  previousTimestamp,
  screenshotUrl,
  showScreenshots,
  showReasoning,
  showState,
}: {
  step: FlowStep;
  previousTimestamp: string | null;
  screenshotUrl: string | null;
  showScreenshots: boolean;
  showReasoning: boolean;
  showState: boolean;
}) {
  const elapsed = elapsedLabel(previousTimestamp, step.timestamp);
  return (
    <div className="border-t py-4 first:border-t-0" data-flow-step={step.index}>
      <div className="flex items-center gap-6 text-xs text-muted-foreground">
        <span>#{step.index}</span>
        {elapsed !== null && (
          <span className="font-mono tabular-nums">{elapsed}</span>
        )}
      </div>
      <div className="mt-2 text-sm">{step.action}</div>
      {step.error !== "" && (
        <div className="mt-2 text-sm text-destructive">{step.error}</div>
      )}
      {showReasoning && (step.expected !== "" || step.observed !== "") && (
        <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
          {step.saw !== "" && <Term label="saw">{step.saw}</Term>}
          {step.expected !== "" && (
            <Term label="expected">{step.expected}</Term>
          )}
          {/* Beside the prediction rather than under the screenshot: the two together are what say
          whether the app behaved as the agent modelled it. */}
          {step.observed !== "" && (
            <Term label="page then">{step.observed}</Term>
          )}
        </dl>
      )}
      {showReasoning &&
        step.expected === "" &&
        step.observed === "" &&
        step.reasoning !== "" && (
          <p className="mt-2 text-sm text-muted-foreground">{step.reasoning}</p>
        )}
      {/* Screenshots are capped rather than full size: a flow runs to a couple of dozen steps, and
      a page of unbounded screenshots is one nobody scrolls to the end of. */}
      {showScreenshots && screenshotUrl !== null && (
        <img
          src={screenshotUrl}
          alt={`Step ${step.index}: ${step.action}`}
          loading="lazy"
          className="mt-3 max-h-[32rem] w-auto max-w-full border object-contain object-left"
        />
      )}
      {showState && step.state !== "" && (
        <div className="mt-3">
          <CodeBlock code={step.state} lang="text" wrap />
        </div>
      )}
    </div>
  );
}

function FlowDetail({
  jobName,
  trialName,
  step,
  flow,
  showScreenshots,
  showReasoning,
  showState,
}: {
  jobName: string;
  trialName: string;
  step?: string | null;
  flow: UiFlow;
  showScreenshots: boolean;
  showReasoning: boolean;
  showState: boolean;
}) {
  const { data: steps, isPending } = useQuery({
    queryKey: ["ui-flow-log", jobName, trialName, step, flow.name],
    queryFn: async () => {
      const log = await fetchTrialFile(
        jobName,
        trialName,
        `${VERIFICATION_DIR}/flows/${flow.name}/log.jsonl`,
        step
      ).catch(() => null);
      return log === null ? [] : parseFlowLog(log);
    },
  });

  // Every record, whatever its kind, so an elapsed time is measured against the line before it
  // rather than against the previous line of the same kind.
  const records = steps ?? [];

  return (
    <div>
      <div className="border-b pb-3">
        <div className="flex items-baseline gap-3">
          <h3 className="text-sm">{flow.name}</h3>
          <span className={cn("text-xs", statusClass(flow.status))}>
            {flow.status}
            {flow.reason !== "" ? ` · ${flow.reason}` : ""}
          </span>
        </div>
        {flow.detail !== "" && (
          <p className="mt-2 whitespace-pre-wrap text-xs text-muted-foreground">
            {flow.detail}
          </p>
        )}
      </div>
      {isPending && (
        <p className="py-4 text-sm text-muted-foreground">Loading flow…</p>
      )}
      {steps !== undefined && steps.length === 0 && !isPending && (
        <p className="py-4 text-sm text-muted-foreground">
          This flow recorded no steps.
        </p>
      )}
      {records.map((flowStep, index) => {
        const screenshotUrl =
          flowStep.screenshot === ""
            ? null
            : flowFileUrl(jobName, trialName, flow.name, flowStep.screenshot, step);
        const key = `${flowStep.kind}-${flowStep.index}`;
        if (flowStep.kind === "init") {
          return (
            <FlowOpening
              key={key}
              step={flowStep}
              screenshotUrl={screenshotUrl}
              showScreenshots={showScreenshots}
              showState={showState}
            />
          );
        }
        if (!KNOWN_KINDS.has(flowStep.kind)) {
          return <UnknownRecord key={key} step={flowStep} />;
        }
        return (
          <FlowStepBlock
            key={key}
            step={flowStep}
            previousTimestamp={
              index > 0 ? records[index - 1].timestamp : null
            }
            screenshotUrl={screenshotUrl}
            showScreenshots={showScreenshots}
            showReasoning={showReasoning}
            showState={showState}
          />
        );
      })}
    </div>
  );
}

export function UiFlowsViewer({
  jobName,
  trialName,
  step,
}: {
  jobName: string;
  trialName: string;
  step?: string | null;
}) {
  const { data: flows, isPending } = useUiFlows(jobName, trialName, step);
  const [selected, setSelected] = useState<string>("");
  const [showScreenshots, setShowScreenshots] = useState(true);
  const [showReasoning, setShowReasoning] = useState(true);
  // The accessibility snapshot is by far the largest thing recorded -- a sixteen-step flow's log is
  // a few hundred kilobytes of it -- so it stays off until asked for.
  const [showState, setShowState] = useState(false);

  const selectedFlow = useMemo(
    () => (flows ?? []).find((flow) => flow.entryId === selected) ?? null,
    [flows, selected]
  );

  useEffect(() => {
    if (selectedFlow === null && flows !== undefined && flows.length > 0) {
      setSelected(flows[0].entryId);
    }
  }, [flows, selectedFlow]);

  if (isPending) {
    return <div className="bg-card border p-6 text-sm text-muted-foreground">Loading…</div>;
  }

  if (flows === undefined || flows.length === 0) {
    return (
      <Empty className="bg-card border">
        <EmptyHeader>
          <EmptyMedia variant="icon">
            <Compass />
          </EmptyMedia>
          <EmptyTitle>No UI flows</EmptyTitle>
          <EmptyDescription>
            This trial declared no UI flows, or its evidence phase never ran.
          </EmptyDescription>
        </EmptyHeader>
      </Empty>
    );
  }

  return (
    <div className="bg-card border">
      <div className="flex items-center justify-between gap-4 border-b px-4 py-2">
        <span className="text-xs uppercase text-muted-foreground">
          {flows.length} flow{flows.length === 1 ? "" : "s"}
        </span>
        <div className="flex items-center gap-1">
          <Toggle
            label="Screenshots"
            pressed={showScreenshots}
            onPressedChange={setShowScreenshots}
          />
          <Toggle
            label="Reasoning"
            pressed={showReasoning}
            onPressedChange={setShowReasoning}
          />
          <Toggle
            label="Page state"
            pressed={showState}
            onPressedChange={setShowState}
          />
        </div>
      </div>
      <div className="flex flex-col sm:flex-row">
        <div className="shrink-0 border-b py-2 sm:w-64 sm:border-b-0 sm:border-r">
          <FlowList
            flows={flows}
            selected={selected}
            onSelect={setSelected}
          />
        </div>
        <div className="min-w-0 flex-1 px-4 py-3">
          {selectedFlow !== null && (
            <FlowDetail
              jobName={jobName}
              trialName={trialName}
              step={step}
              flow={selectedFlow}
              showScreenshots={showScreenshots}
              showReasoning={showReasoning}
              showState={showState}
            />
          )}
        </div>
      </div>
    </div>
  );
}
