/* Notice: a note set in among a page's content -- a sync that stopped, a one-time tip -- in the brand's status
 * shape: a tinted fill with a matching edge on 6px corners, the text in the tone's subtle content. A class
 * builder, like `badgeClass`: a notice holds whatever its page puts in it. The Tailwind scanner reads utility names
 * from the literals in this file: keep every utility name a contiguous literal. */

export type NoticeTone = "neutral" | "info" | "success" | "warn" | "danger";

const NOTICE_TONES: Record<NoticeTone, string> = {
  neutral: "bg-body-surface-raised border-default text-body-content-secondary",
  info: "bg-accent-subtle-surface border-accent-4 text-accent-subtle-content",
  success: "bg-success-subtle-surface border-success-4 text-success-subtle-content",
  warn: "bg-warn-subtle-surface border-warn-4 text-warn-subtle-content",
  danger: "bg-danger-subtle-surface border-danger-4 text-danger-subtle-content",
};

export function noticeClass(tone: NoticeTone, extra = ""): string {
  const parts = ["notice flex items-start gap-2 rounded-md border px-3 py-2 type-body", NOTICE_TONES[tone]];
  if (extra !== "") parts.push(extra);
  return parts.join(" ");
}
