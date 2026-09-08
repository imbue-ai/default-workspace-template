import type { Step } from "~/lib/types";

/**
 * Steps that the minds-evals harness wrote into a trajectory itself, rather than steps the run
 * produced.
 *
 * A stepped task drives one workspace across several instructions, and every step's trajectory
 * replays the conversation from its first turn, so the harness marks each step's first turn. ATIF
 * offers no source for "the harness said this" -- `system` is the closest, and the workspace's own
 * transcript legitimately contributes `system` steps of its own (skill bodies, tens of thousands of
 * characters each). The `extra` namespace is what tells the two apart without matching on prose.
 */
const HARNESS_NAMESPACE = "minds_evals";
const STEP_BOUNDARY = "step_boundary";

export interface HarnessAnnotation {
  kind: string;
  stepName: string | null;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/** The harness annotation on a step, or null for every step the run itself produced. */
export function harnessAnnotation(step: Step): HarnessAnnotation | null {
  const namespace = asRecord(step.extra?.[HARNESS_NAMESPACE]);
  if (namespace === null || typeof namespace.kind !== "string") {
    return null;
  }
  const stepName = namespace.step_name;
  return {
    kind: namespace.kind,
    stepName: typeof stepName === "string" ? stepName : null,
  };
}

export function isStepBoundary(annotation: HarnessAnnotation | null): boolean {
  return annotation?.kind === STEP_BOUNDARY;
}

/**
 * A boundary's message opens with an ASCII rule, which exists so the marker is still findable in a
 * viewer that renders none of this -- stock `harbor view` shows it as an ordinary system step. Here
 * the divider does that job, so the rule is dropped and only the prose beneath it is kept.
 */
function messageProse(step: Step): string {
  const message = typeof step.message === "string" ? step.message : "";
  return message
    .split("\n")
    .filter((line) => line.trim() !== "" && !/^=+ .* =+$/.test(line.trim()))
    .slice(1)
    .join("\n");
}

/**
 * A step boundary drawn as what it is: a seam in the conversation, not something anyone said.
 */
export function StepBoundaryDivider({
  step,
  annotation,
}: {
  step: Step;
  annotation: HarnessAnnotation;
}) {
  const prose = messageProse(step);
  return (
    <div className="py-2" data-step-boundary={annotation.stepName ?? ""}>
      <div className="flex items-center gap-3">
        <span className="h-px flex-1 bg-step-harness/40" aria-hidden="true" />
        <span className="shrink-0 text-xs font-normal uppercase tracking-wide text-step-harness">
          {annotation.stepName ? `Step: ${annotation.stepName}` : "Step boundary"}
        </span>
        <span className="h-px flex-1 bg-step-harness/40" aria-hidden="true" />
      </div>
      {prose !== "" && (
        <p className="mt-2 text-center text-xs text-muted-foreground">{prose}</p>
      )}
    </div>
  );
}
