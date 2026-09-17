/** How a model pick reads where the page names one as a single string: the switch dialog's armed
 *  strip, and the model bar's chip while a switch is carrying the pick out. */

export function modelPickLabel(optionLabel: string, effort: string | null, fast: boolean): string {
  const effortPart = effort === null ? "" : ` · ${capitalizeEffort(effort)}`;
  return `${optionLabel}${effortPart}${fast ? " · fast" : ""}`;
}

export function capitalizeEffort(level: string): string {
  return level.length === 0 ? level : level[0].toUpperCase() + level.slice(1);
}
