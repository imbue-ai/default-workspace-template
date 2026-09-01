/* The full-width dismissable banner box: a bordered strip pinned above its
 * pane's content, message left, actions right. Shared by TerminalBanner
 * (neutral) and UpdateStalenessBanner (warning) so the box geometry cannot
 * drift between them; only the tone tokens differ. The marker is a bare class
 * (like `btn`) so the vitest suites can find each banner. */

export type BannerTone = "neutral" | "warning";

const BANNER_TONES: Record<BannerTone, string> = {
  neutral: "border-default bg-surface-secondary text-secondary",
  warning: "border-warning bg-warning-surface text-primary",
};

export function bannerClass(marker: string, tone: BannerTone): string {
  return (
    `${marker} flex flex-none items-center justify-between gap-3 border-b px-2.5 py-1.5 ` +
    `text-(length:--font-size-body) leading-[1.4] ${BANNER_TONES[tone]}`
  );
}
