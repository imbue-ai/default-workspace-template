/**
 * What the chat root's nested pages last said of an agent working, and what the root tells the shell of them.
 *
 * Each chat page says when a reply starts and stops being under way in its chat (``shell:agent-working``). The shell
 * hears every nested page as the root, one frame, so the root keeps each page's last word itself and says one thing:
 * that an agent is working while any page it holds says so. A page that goes away takes its word with it.
 */

export interface AgentWorkingReports<Page> {
  /** What ``page`` now says. */
  note(page: Page, isWorking: boolean): void;
  /** ``page`` is gone, or is a fresh page that has said nothing yet. */
  forget(page: Page): void;
  /** Whether any page's last word is that an agent is working. */
  isAnyWorking(): boolean;
}

/** Reports that tell ``onChange`` each time the pages together change their answer, starting from nobody working. */
export function makeAgentWorkingReports<Page>(onChange: (isAnyWorking: boolean) => void): AgentWorkingReports<Page> {
  const working = new Set<Page>();
  let wasAnyWorking = false;
  const tell = (): void => {
    const isAnyWorking = working.size > 0;
    if (isAnyWorking === wasAnyWorking) return;
    wasAnyWorking = isAnyWorking;
    onChange(isAnyWorking);
  };
  return {
    note: (page, isWorking) => {
      if (isWorking) working.add(page);
      else working.delete(page);
      tell();
    },
    forget: (page) => {
      working.delete(page);
      tell();
    },
    isAnyWorking: () => working.size > 0,
  };
}
