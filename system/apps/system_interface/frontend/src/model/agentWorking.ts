/**
 * Whether an agent is at work, as the shell hears it from two places.
 *
 * One is the avatar's status, which follows the workspace's agent events and is prompt only for an agent that
 * reports its activity there. The other is the pages the shell frames: a page that shows an agent's work says when
 * one is working in it (``shell:agent-working``), whichever agent that is. An agent counts as working while either
 * says so, and the show-around tour begins, resumes and pauses on the two together.
 */

export interface AgentWorkingSignal {
  /** What the avatar's status says. */
  noteAvatar(isWorking: boolean): void;
  /** Whether the last report of any page the shell frames says an agent is working. */
  notePages(isWorking: boolean): void;
}

/** A signal that tells ``onChange`` each time the two together change their answer, starting from nobody working. */
export function makeAgentWorkingSignal(onChange: (isWorking: boolean) => void): AgentWorkingSignal {
  let isAvatarWorking = false;
  let isAnyPageWorking = false;
  let wasWorking = false;
  const tell = (): void => {
    const isWorking = isAvatarWorking || isAnyPageWorking;
    if (isWorking === wasWorking) return;
    wasWorking = isWorking;
    onChange(isWorking);
  };
  return {
    noteAvatar: (isWorking) => {
      isAvatarWorking = isWorking;
      tell();
    },
    notePages: (isWorking) => {
      isAnyPageWorking = isWorking;
      tell();
    },
  };
}
